"""
Analytics mixin for LTMActor - provides emotional analysis and pattern detection
"""
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
import json
import numpy as np
from models.ltm_models import LTMEntry
from actors.events.ltm_events import EmotionalPatternDetectedEvent, AnalyticsGeneratedEvent
from config.settings import (
    LTM_QUERY_TIMEOUT,
    EMOTION_LABELS,
    LTM_ANALYTICS_SIMILARITY_THRESHOLD,
    LTM_ANALYTICS_PATTERN_MIN_OCCURRENCES,
    LTM_ANALYTICS_TRAJECTORY_DEFAULT_DAYS,
    LTM_ANALYTICS_TRAJECTORY_DEFAULT_GRANULARITY,
    LTM_ANALYTICS_CONCEPT_ASSOCIATIONS_LIMIT,
    LTM_ANALYTICS_EMOTIONAL_SIMILARITY_LIMIT
)


class LTMAnalyticsMixin:
    """Mixin providing analytics methods for LTM"""
    
    # These attributes are available from LTMActor
    _pool: Optional[object]
    _degraded_mode: bool
    logger: object
    _event_version_manager: object
    get_actor_system: callable
    actor_id: str
    
    async def get_emotional_pattern(
        self,
        user_id: str,
        concept: str,
        time_window_days: Optional[int] = None
    ) -> Dict[str, float]:
        """
        Extract emotional associations with a concept
        
        Args:
            user_id: User ID
            concept: Concept to analyze (searched in semantic_tags and conversation_fragment)
            time_window_days: Optional time window for analysis
            
        Returns:
            Averaged emotional profile for the concept
        """
        if self._degraded_mode:
            return {}
            
        if not self._pool:
            self.logger.error("Database pool not initialized")
            return {}
            
        try:
            # Build time filter
            time_filter = ""
            params = [user_id, f"%{concept.lower()}%", concept.lower()]
            
            if time_window_days:
                cutoff = datetime.now(timezone.utc) - timedelta(days=time_window_days)
                time_filter = "AND created_at >= $4"
                params.append(cutoff)
            
            # Query memories containing the concept
            query = f"""
                SELECT emotional_snapshot
                FROM ltm_memories
                WHERE user_id = $1 
                AND (
                    conversation_fragment::text ILIKE $2
                    OR $3 = ANY(semantic_tags)
                )
                {time_filter}
            """
            
            rows = await self._pool.fetch(query, *params, timeout=LTM_QUERY_TIMEOUT)
            
            if not rows:
                return {}
            
            # Aggregate emotional snapshots
            aggregated = self._aggregate_emotional_vectors(
                [json.loads(row['emotional_snapshot']) for row in rows]
            )
            
            # Generate event if significant pattern found
            if len(rows) >= LTM_ANALYTICS_PATTERN_MIN_OCCURRENCES:
                event = EmotionalPatternDetectedEvent.create(
                    user_id=user_id,
                    pattern_type='association',
                    pattern_data={
                        'concept': concept,
                        'emotional_profile': aggregated,
                        'occurrence_count': len(rows)
                    },
                    confidence=min(len(rows) / 10.0, 1.0)  # Simple confidence metric
                )
                await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return aggregated
            
        except Exception as e:
            self.logger.error(f"Failed to get emotional pattern: {str(e)}")
            return {}
    
    async def search_by_emotional_similarity(
        self,
        target_emotions: Dict[str, float],
        user_id: str,
        threshold: float = LTM_ANALYTICS_SIMILARITY_THRESHOLD,
        limit: int = LTM_ANALYTICS_EMOTIONAL_SIMILARITY_LIMIT
    ) -> List[LTMEntry]:
        """
        Search memories by emotional similarity using cosine similarity
        
        Args:
            target_emotions: Target emotional vector
            user_id: User ID
            threshold: Minimum similarity threshold (0.0-1.0)
            limit: Maximum number of results
            
        Returns:
            List of LTMEntry objects sorted by emotional similarity
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        try:
            # Get all memories with emotional snapshots for the user
            query = """
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at
                FROM ltm_memories
                WHERE user_id = $1
            """
            
            rows = await self._pool.fetch(query, user_id, timeout=LTM_QUERY_TIMEOUT)
            
            if not rows:
                return []
            
            # Convert target emotions to numpy array
            target_vector = np.array([target_emotions.get(emotion, 0.0) for emotion in EMOTION_LABELS])
            
            # Calculate similarities and filter
            similar_memories = []
            for row in rows:
                # Parse emotional snapshot
                emotional_data = json.loads(row['emotional_snapshot'])
                current_vector = np.array([emotional_data.get(emotion, 0.0) for emotion in EMOTION_LABELS])
                
                # Calculate cosine similarity
                similarity = self._calculate_emotional_similarity(
                    dict(zip(EMOTION_LABELS, target_vector)),
                    dict(zip(EMOTION_LABELS, current_vector))
                )
                
                if similarity >= threshold:
                    # Convert to LTMEntry and add similarity score
                    entry = self._row_to_ltm_entry(row)
                    if entry:
                        similar_memories.append((entry, similarity))
            
            # Sort by similarity and limit
            similar_memories.sort(key=lambda x: x[1], reverse=True)
            results = [entry for entry, _ in similar_memories[:limit]]
            
            # Update access counts
            if results:
                memory_ids = [entry.memory_id for entry in results if entry.memory_id]
                await self._update_access_counts(memory_ids)
            
            return results
            
        except Exception as e:
            self.logger.error(f"Failed to search by emotional similarity: {str(e)}")
            return []
    
    async def get_dominant_emotions_history(
        self,
        user_id: str,
        time_window_days: int = LTM_ANALYTICS_TRAJECTORY_DEFAULT_DAYS,
        top_n: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Get history of dominant emotions using SQL function
        
        Args:
            user_id: User ID
            time_window_days: Days to look back
            top_n: Number of top emotions to return
            
        Returns:
            List of emotion statistics with avg_intensity and occurrence_count
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        try:
            # Use the SQL function from migration 005
            query = """
                SELECT * FROM get_user_emotional_stats($1, $2)
                LIMIT $3
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                time_window_days,
                top_n,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            # Format results
            results = []
            for row in rows:
                results.append({
                    'emotion': row['emotion'],
                    'avg_intensity': float(row['avg_intensity']),
                    'occurrence_count': int(row['occurrence_count'])
                })
            
            return results
            
        except Exception as e:
            self.logger.error(f"Failed to get dominant emotions history: {str(e)}")
            return []
    
    async def get_emotional_trajectory(
        self,
        user_id: str,
        time_window_days: int = LTM_ANALYTICS_TRAJECTORY_DEFAULT_DAYS,
        granularity: str = LTM_ANALYTICS_TRAJECTORY_DEFAULT_GRANULARITY
    ) -> List[Dict[str, Any]]:
        """
        Build emotional trajectory over time
        
        Args:
            user_id: User ID
            time_window_days: Days to analyze
            granularity: Time granularity ('hour', 'day', 'week')
            
        Returns:
            List of time periods with emotional states and trends
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        # Validate granularity
        if granularity not in ['hour', 'day', 'week']:
            granularity = 'day'
            
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=time_window_days)
            
            # Get memories grouped by time period
            query = """
                SELECT 
                    date_trunc($2, created_at) as period,
                    jsonb_agg(emotional_snapshot) as snapshots,
                    COUNT(*) as memory_count,
                    AVG(emotional_intensity) as avg_intensity
                FROM ltm_memories
                WHERE user_id = $1 AND created_at >= $3
                GROUP BY date_trunc($2, created_at)
                ORDER BY period ASC
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                granularity,
                cutoff,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            if not rows:
                return []
            
            # Process each period
            trajectory = []
            emotion_trends = {emotion: [] for emotion in EMOTION_LABELS}
            
            for row in rows:
                # Aggregate emotions for this period
                period_emotions = self._aggregate_emotional_vectors(
                    [json.loads(snapshot) for snapshot in row['snapshots']]
                )
                
                trajectory.append({
                    'timestamp': row['period'].isoformat(),
                    'emotions': period_emotions,
                    'intensity': float(row['avg_intensity']),
                    'memory_count': int(row['memory_count'])
                })
                
                # Track trends
                for emotion, value in period_emotions.items():
                    emotion_trends[emotion].append(value)
            
            # Detect trends
            trends = self._detect_emotional_trends(emotion_trends)
            
            # Generate event if significant trajectory found
            if len(trajectory) >= 3:  # At least 3 points for a trajectory
                event = EmotionalPatternDetectedEvent.create(
                    user_id=user_id,
                    pattern_type='trajectory',
                    pattern_data={
                        'period': f"{cutoff.date()} to {datetime.now().date()}",
                        'granularity': granularity,
                        'points_count': len(trajectory),
                        'trends': trends
                    },
                    confidence=0.8
                )
                await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return {
                'user_id': user_id,
                'period': f"{cutoff.date()} to {datetime.now().date()}",
                'granularity': granularity,
                'points': trajectory,
                'trends': trends
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get emotional trajectory: {str(e)}")
            return []
    
    async def get_concept_associations(
        self,
        concept: str,
        limit: int = LTM_ANALYTICS_CONCEPT_ASSOCIATIONS_LIMIT
    ) -> Dict[str, Any]:
        """
        Analyze what a concept is associated with across all users
        
        Args:
            concept: Concept to analyze
            limit: Maximum number of memories to analyze
            
        Returns:
            Structured analysis of concept associations
        """
        if self._degraded_mode:
            return {}
            
        if not self._pool:
            return {}
            
        try:
            # Find memories with the concept
            query = """
                SELECT 
                    semantic_tags,
                    emotional_snapshot,
                    memory_type,
                    trigger_reason,
                    dominant_emotions,
                    importance_score
                FROM ltm_memories
                WHERE $1 = ANY(semantic_tags)
                LIMIT $2
            """
            
            rows = await self._pool.fetch(
                query,
                concept.lower(),
                limit,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            if not rows:
                return {'concept': concept, 'found': False}
            
            # Analyze associations
            related_tags = {}
            memory_types = {}
            trigger_reasons = {}
            emotional_profile = []
            importance_scores = []
            
            for row in rows:
                # Count related tags
                for tag in row['semantic_tags']:
                    if tag != concept.lower():
                        related_tags[tag] = related_tags.get(tag, 0) + 1
                
                # Count memory types
                memory_types[row['memory_type']] = memory_types.get(row['memory_type'], 0) + 1
                
                # Count trigger reasons
                trigger_reasons[row['trigger_reason']] = trigger_reasons.get(row['trigger_reason'], 0) + 1
                
                # Collect emotional data
                emotional_profile.append(json.loads(row['emotional_snapshot']))
                importance_scores.append(row['importance_score'])
            
            # Aggregate emotions
            avg_emotions = self._aggregate_emotional_vectors(emotional_profile)
            
            # Sort related concepts by frequency
            sorted_tags = sorted(related_tags.items(), key=lambda x: x[1], reverse=True)
            
            return {
                'concept': concept,
                'found': True,
                'occurrence_count': len(rows),
                'related_concepts': [{'tag': tag, 'count': count} for tag, count in sorted_tags[:10]],
                'memory_types': memory_types,
                'trigger_reasons': trigger_reasons,
                'emotional_profile': avg_emotions,
                'avg_importance': sum(importance_scores) / len(importance_scores) if importance_scores else 0.0
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get concept associations: {str(e)}")
            return {'concept': concept, 'error': str(e)}
    
    async def detect_emotional_patterns(
        self,
        user_id: str,
        min_occurrences: int = LTM_ANALYTICS_PATTERN_MIN_OCCURRENCES
    ) -> List[Dict[str, Any]]:
        """
        Detect recurring emotional patterns for a user
        
        Args:
            user_id: User ID
            min_occurrences: Minimum number of occurrences to consider a pattern
            
        Returns:
            List of detected patterns with their characteristics
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        try:
            # Get all emotional data for the user
            query = """
                SELECT 
                    dominant_emotions,
                    trigger_reason,
                    emotional_intensity,
                    created_at
                FROM ltm_memories
                WHERE user_id = $1
                ORDER BY created_at DESC
            """
            
            rows = await self._pool.fetch(query, user_id, timeout=LTM_QUERY_TIMEOUT)
            
            if len(rows) < min_occurrences:
                return []
            
            # Analyze patterns
            patterns = []
            
            # 1. Emotion combinations pattern
            emotion_combos = {}
            for row in rows:
                combo = tuple(sorted(row['dominant_emotions'][:3]))  # Top 3 emotions
                if combo:
                    emotion_combos[combo] = emotion_combos.get(combo, 0) + 1
            
            # Filter by min occurrences
            recurring_combos = {
                combo: count for combo, count in emotion_combos.items()
                if count >= min_occurrences
            }
            
            for combo, count in recurring_combos.items():
                patterns.append({
                    'type': 'emotion_combination',
                    'pattern': list(combo),
                    'occurrences': count,
                    'frequency': count / len(rows)
                })
            
            # 2. Trigger patterns
            trigger_emotions = {}
            for row in rows:
                trigger = row['trigger_reason']
                if trigger not in trigger_emotions:
                    trigger_emotions[trigger] = []
                trigger_emotions[trigger].extend(row['dominant_emotions'])
            
            # Find consistent trigger-emotion associations
            for trigger, emotions in trigger_emotions.items():
                if len(emotions) >= min_occurrences:
                    emotion_counts = {}
                    for emotion in emotions:
                        emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
                    
                    # Find emotions that appear >50% of the time with this trigger
                    consistent_emotions = [
                        emotion for emotion, count in emotion_counts.items()
                        if count / len(emotions) > 0.5
                    ]
                    
                    if consistent_emotions:
                        patterns.append({
                            'type': 'trigger_emotion',
                            'trigger': trigger,
                            'associated_emotions': consistent_emotions,
                            'strength': max(count / len(emotions) for emotion, count in emotion_counts.items())
                        })
            
            # Generate event if patterns found
            if patterns:
                event = EmotionalPatternDetectedEvent.create(
                    user_id=user_id,
                    pattern_type='recurring',
                    pattern_data={
                        'patterns_count': len(patterns),
                        'patterns': patterns[:5]  # Top 5 patterns
                    },
                    confidence=0.9
                )
                await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return patterns
            
        except Exception as e:
            self.logger.error(f"Failed to detect emotional patterns: {str(e)}")
            return []
    
    async def search_memories_by_mood(
        self,
        user_id: str,
        mood_vector: Dict[str, float],
        time_range: Optional[Tuple[datetime, datetime]] = None,
        limit: int = 10
    ) -> List[LTMEntry]:
        """
        Search memories by overall mood combining emotional similarity with time filter
        
        Args:
            user_id: User ID
            mood_vector: Target mood as emotional vector
            time_range: Optional time range filter
            limit: Maximum number of results
            
        Returns:
            List of LTMEntry objects matching the mood
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        try:
            # Build query with optional time filter
            time_filter = ""
            params = [user_id]
            
            if time_range:
                time_filter = "AND created_at >= $2 AND created_at <= $3"
                params.extend(time_range)
            
            query = f"""
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at
                FROM ltm_memories
                WHERE user_id = $1 {time_filter}
            """
            
            rows = await self._pool.fetch(query, *params, timeout=LTM_QUERY_TIMEOUT)
            
            if not rows:
                return []
            
            # Calculate mood similarities weighted by emotional intensity
            mood_matches = []
            for row in rows:
                emotional_data = json.loads(row['emotional_snapshot'])
                similarity = self._calculate_emotional_similarity(mood_vector, emotional_data)
                
                # Weight by emotional intensity
                weighted_score = similarity * row['emotional_intensity']
                
                if weighted_score > 0.3:  # Threshold for mood match
                    entry = self._row_to_ltm_entry(row)
                    if entry:
                        mood_matches.append((entry, weighted_score))
            
            # Sort by weighted score and limit
            mood_matches.sort(key=lambda x: x[1], reverse=True)
            results = [entry for entry, _ in mood_matches[:limit]]
            
            return results
            
        except Exception as e:
            self.logger.error(f"Failed to search by mood: {str(e)}")
            return []
    
    async def calculate_memory_importance_distribution(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Calculate distribution of memory importance scores
        
        Args:
            user_id: User ID
            
        Returns:
            Statistics about importance score distribution
        """
        if self._degraded_mode:
            return {}
            
        if not self._pool:
            return {}
            
        try:
            # Get importance statistics
            query = """
                SELECT 
                    COUNT(*) as total_count,
                    AVG(importance_score) as avg_importance,
                    STDDEV(importance_score) as stddev_importance,
                    MIN(importance_score) as min_importance,
                    MAX(importance_score) as max_importance,
                    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY importance_score) as q1,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY importance_score) as median,
                    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY importance_score) as q3
                FROM ltm_memories
                WHERE user_id = $1
            """
            
            stats = await self._pool.fetchrow(query, user_id, timeout=LTM_QUERY_TIMEOUT)
            
            if not stats or stats['total_count'] == 0:
                return {'user_id': user_id, 'total_memories': 0}
            
            # Get histogram data
            histogram_query = """
                SELECT 
                    WIDTH_BUCKET(importance_score, 0, 1, 10) as bucket,
                    COUNT(*) as count,
                    AVG(importance_score) as avg_score
                FROM ltm_memories
                WHERE user_id = $1
                GROUP BY bucket
                ORDER BY bucket
            """
            
            histogram_rows = await self._pool.fetch(histogram_query, user_id, timeout=LTM_QUERY_TIMEOUT)
            
            # Format histogram
            histogram = []
            for row in histogram_rows:
                bucket_start = (row['bucket'] - 1) * 0.1
                bucket_end = row['bucket'] * 0.1
                histogram.append({
                    'range': f"{bucket_start:.1f}-{bucket_end:.1f}",
                    'count': row['count'],
                    'avg_score': float(row['avg_score'])
                })
            
            # Detect anomalies (scores > 2 std dev from mean)
            anomaly_threshold = float(stats['avg_importance']) + 2 * float(stats['stddev_importance'] or 0)
            
            anomalies_query = """
                SELECT COUNT(*) as anomaly_count
                FROM ltm_memories
                WHERE user_id = $1 AND importance_score > $2
            """
            
            anomaly_count = await self._pool.fetchval(
                anomalies_query,
                user_id,
                anomaly_threshold,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            # Generate analytics event
            event = AnalyticsGeneratedEvent.create(
                user_id=user_id,
                analytics_type='importance_distribution',
                data={
                    'total_memories': stats['total_count'],
                    'avg_importance': float(stats['avg_importance']),
                    'anomaly_count': anomaly_count
                }
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return {
                'user_id': user_id,
                'total_memories': stats['total_count'],
                'statistics': {
                    'mean': float(stats['avg_importance']),
                    'std_dev': float(stats['stddev_importance'] or 0),
                    'min': float(stats['min_importance']),
                    'max': float(stats['max_importance']),
                    'quartiles': {
                        'q1': float(stats['q1']),
                        'median': float(stats['median']),
                        'q3': float(stats['q3'])
                    }
                },
                'histogram': histogram,
                'anomalies': {
                    'threshold': anomaly_threshold,
                    'count': anomaly_count,
                    'percentage': (anomaly_count / stats['total_count']) * 100
                }
            }
            
        except Exception as e:
            self.logger.error(f"Failed to calculate importance distribution: {str(e)}")
            return {'user_id': user_id, 'error': str(e)}
    
    def _calculate_emotional_similarity(
        self,
        emotions1: Dict[str, float],
        emotions2: Dict[str, float]
    ) -> float:
        """
        Calculate cosine similarity between two emotional vectors
        
        Args:
            emotions1: First emotional vector
            emotions2: Second emotional vector
            
        Returns:
            Cosine similarity (0.0-1.0)
        """
        # Convert to numpy arrays ensuring same order
        vec1 = np.array([emotions1.get(emotion, 0.0) for emotion in EMOTION_LABELS])
        vec2 = np.array([emotions2.get(emotion, 0.0) for emotion in EMOTION_LABELS])
        
        # Handle zero vectors
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        # Calculate cosine similarity
        return float(np.dot(vec1, vec2) / (norm1 * norm2))
    
    def _aggregate_emotional_vectors(
        self,
        vectors: List[Dict[str, float]],
        weights: Optional[List[float]] = None
    ) -> Dict[str, float]:
        """
        Aggregate multiple emotional vectors into one
        
        Args:
            vectors: List of emotional vectors
            weights: Optional weights for each vector
            
        Returns:
            Aggregated emotional vector
        """
        if not vectors:
            return {emotion: 0.0 for emotion in EMOTION_LABELS}
        
        if weights and len(weights) != len(vectors):
            raise ValueError("Weights length must match vectors length")
        
        # Default weights if not provided
        if not weights:
            weights = [1.0] * len(vectors)
        
        # Sum weighted vectors
        aggregated = {emotion: 0.0 for emotion in EMOTION_LABELS}
        total_weight = sum(weights)
        
        for vector, weight in zip(vectors, weights):
            for emotion in EMOTION_LABELS:
                aggregated[emotion] += vector.get(emotion, 0.0) * weight
        
        # Normalize by total weight
        if total_weight > 0:
            for emotion in aggregated:
                aggregated[emotion] /= total_weight
                # Round to 3 decimal places
                aggregated[emotion] = round(aggregated[emotion], 3)
        
        return aggregated
    
    def _detect_emotional_trends(
        self,
        emotion_trends: Dict[str, List[float]]
    ) -> Dict[str, List[str]]:
        """
        Detect trends in emotional data over time
        
        Args:
            emotion_trends: Dictionary of emotion -> list of values over time
            
        Returns:
            Dictionary with increasing, decreasing, and stable emotions
        """
        trends = {
            'increasing': [],
            'decreasing': [],
            'stable': []
        }
        
        for emotion, values in emotion_trends.items():
            if len(values) < 2:
                continue
            
            # Simple linear trend detection
            # Calculate average change
            changes = [values[i+1] - values[i] for i in range(len(values)-1)]
            avg_change = sum(changes) / len(changes) if changes else 0
            
            # Categorize based on average change
            if avg_change > 0.05:  # Increasing threshold
                trends['increasing'].append(emotion)
            elif avg_change < -0.05:  # Decreasing threshold
                trends['decreasing'].append(emotion)
            else:
                trends['stable'].append(emotion)
        
        return trends
    
    def _row_to_ltm_entry(self, row: Any) -> Optional[LTMEntry]:
        """
        Convert database row to LTMEntry object
        
        Args:
            row: asyncpg Record object
            
        Returns:
            LTMEntry object or None if conversion fails
        """
        try:
            # Convert row to dict
            entry_dict = dict(row)
            
            # Parse JSON fields
            if isinstance(entry_dict.get('conversation_fragment'), str):
                entry_dict['conversation_fragment'] = json.loads(entry_dict['conversation_fragment'])
            
            if isinstance(entry_dict.get('emotional_snapshot'), str):
                entry_dict['emotional_snapshot'] = json.loads(entry_dict['emotional_snapshot'])
            
            # Create LTMEntry
            return LTMEntry(**entry_dict)
            
        except Exception as e:
            self.logger.error(f"Failed to convert row to LTMEntry: {str(e)}")
            return None
    
    async def _update_access_counts(self, memory_ids: List[Any]) -> None:
        """
        Update access counts for retrieved memories
        
        Args:
            memory_ids: List of memory UUIDs
        """
        if not memory_ids or not self._pool:
            return
            
        try:
            query = """
                UPDATE ltm_memories
                SET 
                    accessed_count = accessed_count + 1,
                    last_accessed_at = CURRENT_TIMESTAMP
                WHERE memory_id = ANY($1::uuid[])
            """
            
            await self._pool.execute(query, memory_ids, timeout=LTM_QUERY_TIMEOUT)
            
        except Exception as e:
            self.logger.warning(f"Failed to update access counts: {str(e)}")