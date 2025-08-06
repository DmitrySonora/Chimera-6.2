"""
Novelty assessment mixin for LTMActor - provides user profile management for novelty calculation
"""
from typing import Optional, Dict, List, Tuple
import json
from datetime import datetime, timezone
from math import exp
import numpy as np
from models.ltm_models import (
    LTMUserProfile, LTMEntry, ConversationFragment, Message, 
    EmotionalSnapshot, MemoryType, TriggerReason
)
from config.settings import (
    LTM_NOVELTY_SEMANTIC_WEIGHT,
    LTM_NOVELTY_EMOTIONAL_WEIGHT,
    LTM_NOVELTY_CONTEXT_WEIGHT,
    LTM_NOVELTY_TEMPORAL_WEIGHT,
    LTM_KNN_NEIGHBORS,
    LTM_KNN_DENSITY_THRESHOLD,
    LTM_KNN_DENSITY_PENALTY,
    LTM_QUERY_TIMEOUT
)


class LTMNoveltyMixin:
    """Mixin providing user profile management for novelty assessment"""
    
    # These attributes are available from LTMActor
    _pool: Optional[object]
    _degraded_mode: bool
    logger: object
    
    async def _get_user_profile(self, user_id: str) -> Optional[LTMUserProfile]:
        """
        Load user profile from database
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            LTMUserProfile object or None if not found
        """
        if self._degraded_mode:
            return None
            
        if not self._pool:
            self.logger.error("Database pool not initialized")
            return None
            
        try:
            query = """
                SELECT user_id, total_messages, calibration_complete,
                       emotion_frequencies, tag_frequencies, recent_novelty_scores,
                       current_percentile_90, last_memory_timestamp,
                       created_at, updated_at
                FROM ltm_user_profiles
                WHERE user_id = $1
            """
            
            row = await self._pool.fetchrow(query, user_id, timeout=LTM_QUERY_TIMEOUT)
            
            if not row:
                return None
            
            # Convert row to dict and create model
            profile_data = dict(row)
            
            # Parse JSON strings to dictionaries
            if isinstance(profile_data.get('emotion_frequencies'), str):
                profile_data['emotion_frequencies'] = json.loads(profile_data['emotion_frequencies'])
            if isinstance(profile_data.get('tag_frequencies'), str):
                profile_data['tag_frequencies'] = json.loads(profile_data['tag_frequencies'])
                
            profile = LTMUserProfile(**profile_data)
            
            self.logger.debug(f"User profile loaded for user_id: {user_id}")
            return profile
            
        except Exception as e:
            self.logger.error(f"Failed to get user profile for {user_id}: {str(e)}")
            raise
    
    async def _create_user_profile(self, user_id: str) -> LTMUserProfile:
        """
        Create new user profile with default values
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            Newly created LTMUserProfile
            
        Raises:
            Exception: If creation fails
        """
        if self._degraded_mode:
            raise RuntimeError("Cannot create profile in degraded mode")
            
        if not self._pool:
            raise RuntimeError("Database pool not initialized")
            
        try:
            # Create new profile with defaults
            new_profile = LTMUserProfile(user_id=user_id)
            
            # Try to insert with ON CONFLICT DO NOTHING
            query = """
                INSERT INTO ltm_user_profiles (
                    user_id, total_messages, calibration_complete,
                    emotion_frequencies, tag_frequencies, recent_novelty_scores,
                    current_percentile_90, last_memory_timestamp
                ) VALUES (
                    $1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8
                ) ON CONFLICT (user_id) DO NOTHING
                RETURNING user_id
            """
            
            result = await self._pool.fetchval(
                query,
                new_profile.user_id,
                new_profile.total_messages,
                new_profile.calibration_complete,
                json.dumps(new_profile.emotion_frequencies),
                json.dumps(new_profile.tag_frequencies),
                new_profile.recent_novelty_scores,
                new_profile.current_percentile_90,
                new_profile.last_memory_timestamp,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            # If insert succeeded
            if result:
                self.logger.debug(f"User profile created for user_id: {user_id}")
                return new_profile
            
            # If conflict occurred, fetch existing profile
            existing_profile = await self._get_user_profile(user_id)
            if existing_profile:
                return existing_profile
            
            # This shouldn't happen, but handle it
            raise RuntimeError(f"Failed to create or fetch profile for user {user_id}")
            
        except Exception as e:
            self.logger.error(f"Failed to create user profile for {user_id}: {str(e)}")
            raise
    
    async def _update_user_profile(self, profile: LTMUserProfile) -> None:
        """
        Update existing user profile in database
        
        Args:
            profile: LTMUserProfile to update
            
        Note:
            updated_at is handled by database trigger
        """
        if self._degraded_mode:
            return
            
        if not self._pool:
            self.logger.error("Cannot update profile - database pool not initialized")
            return
            
        try:
            # Note: NOT updating updated_at - trigger will handle it
            query = """
                UPDATE ltm_user_profiles SET
                    total_messages = $2,
                    calibration_complete = $3,
                    emotion_frequencies = $4::jsonb,
                    tag_frequencies = $5::jsonb,
                    recent_novelty_scores = $6,
                    current_percentile_90 = $7,
                    last_memory_timestamp = $8
                WHERE user_id = $1
            """
            
            await self._pool.execute(
                query,
                profile.user_id,
                profile.total_messages,
                profile.calibration_complete,
                json.dumps(profile.emotion_frequencies),
                json.dumps(profile.tag_frequencies),
                profile.recent_novelty_scores,
                profile.current_percentile_90,
                profile.last_memory_timestamp,
                timeout=LTM_QUERY_TIMEOUT
            )
            
        except Exception as e:
            self.logger.error(f"Failed to update user profile for {profile.user_id}: {str(e)}")
            raise
    
    async def _get_or_create_profile(self, user_id: str) -> LTMUserProfile:
        """
        Get existing profile or create new one if doesn't exist
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            LTMUserProfile (existing or newly created)
            
        Raises:
            Exception: If both get and create fail
        """
        # Try to get existing profile first
        profile = await self._get_user_profile(user_id)
        if profile:
            return profile
        
        # Create new profile
        return await self._create_user_profile(user_id)
    
    async def _update_emotion_frequencies(self, profile: LTMUserProfile, emotions: Dict[str, float]) -> None:
        """
        Update emotion frequency counters
        
        Args:
            profile: User profile to update
            emotions: Dict of emotion -> intensity (0.0-1.0)
        """
        from config.settings import LTM_EMOTION_FREQUENCY_THRESHOLD
        
        for emotion, intensity in emotions.items():
            # Only count emotions above threshold
            if intensity > LTM_EMOTION_FREQUENCY_THRESHOLD:
                if emotion not in profile.emotion_frequencies:
                    profile.emotion_frequencies[emotion] = 0
                profile.emotion_frequencies[emotion] += 1
    
    async def _update_tag_frequencies(self, profile: LTMUserProfile, tags: List[str]) -> None:
        """
        Update semantic tag frequency counters
        
        Args:
            profile: User profile to update
            tags: List of semantic tags (already normalized)
        """
        for tag in tags:
            if tag not in profile.tag_frequencies:
                profile.tag_frequencies[tag] = 0
            profile.tag_frequencies[tag] += 1
    
    async def _update_novelty_scores(self, profile: LTMUserProfile, score: float) -> None:
        """
        Update sliding window of novelty scores
        
        Args:
            profile: User profile to update
            score: Novelty score to add (0.0-1.0)
        """
        from config.settings import LTM_NOVELTY_SCORES_WINDOW
        
        # Add new score
        profile.recent_novelty_scores.append(score)
        
        # Maintain window size (FIFO)
        if len(profile.recent_novelty_scores) > LTM_NOVELTY_SCORES_WINDOW:
            profile.recent_novelty_scores.pop(0)
        
        # Recalculate percentile after update
        await self._recalculate_percentile(profile)
    
    async def _recalculate_percentile(self, profile: LTMUserProfile) -> None:
        """
        Recalculate 90th percentile from recent scores
        
        Args:
            profile: User profile to update
        """
        from config.settings import LTM_PERCENTILE_MIN_SAMPLES
        
        # Only calculate if we have enough samples
        if len(profile.recent_novelty_scores) < LTM_PERCENTILE_MIN_SAMPLES:
            return
        
        # Sort a copy to find percentile
        sorted_scores = sorted(profile.recent_novelty_scores)
        
        # Calculate 90th percentile index
        percentile_index = int(len(sorted_scores) * 0.9)
        
        # Update profile
        profile.current_percentile_90 = sorted_scores[percentile_index]
    
    async def _update_profile_statistics(
        self, 
        profile: LTMUserProfile, 
        emotions: Dict[str, float], 
        tags: List[str], 
        novelty_score: float
    ) -> None:
        """
        Update all profile statistics and save to database
        
        Args:
            profile: User profile to update
            emotions: Emotion intensities
            tags: Semantic tags
            novelty_score: Calculated novelty score
        """
        # Update all statistics
        await self._update_emotion_frequencies(profile, emotions)
        await self._update_tag_frequencies(profile, tags)
        await self._update_novelty_scores(profile, novelty_score)
        
        # Increment message counter
        profile.total_messages += 1
        
        # Save to database
        await self._update_user_profile(profile)
    
    async def calculate_novelty_score(
        self, 
        user_id: str,
        text: str,
        emotions: Dict[str, float],
        tags: List[str],
        profile: LTMUserProfile
    ) -> Tuple[float, Dict[str, float]]:
        """
        Calculate multi-factor novelty score for a potential memory
        
        Args:
            user_id: User ID
            text: Text content for embedding generation
            emotions: Emotion intensities dictionary
            tags: Semantic tags list
            profile: User profile with statistics
            
        Returns:
            Tuple of (final_score, factor_details)
            where factor_details contains scores for each factor
        """
        # Initialize factor scores
        factor_details = {
            "semantic": 0.0,
            "emotional": 0.0,
            "contextual": 0.0,
            "temporal": 0.0,
            "density_modifier": 1.0
        }
        
        try:
            # === Factor 1: Semantic Distance (40%) ===
            semantic_novelty = 1.0  # Default for new users
            distances = []
            
            # Generate embedding for current text
            embedding = await self._generate_embedding_for_novelty(
                user_id, text, emotions, tags
            )
            
            if embedding is not None:
                # Get nearest neighbors with distances
                neighbors_with_distances = await self._get_nearest_neighbors(
                    user_id, embedding, limit=10
                )
                
                if len(neighbors_with_distances) >= 5:
                    # Calculate average distance to top-5
                    distances = [dist for _, dist in neighbors_with_distances[:5]]
                    semantic_novelty = sum(distances) / len(distances)
                # If < 5 memories, everything is new (novelty = 1.0)
            
            factor_details["semantic"] = semantic_novelty
            
            # === Factor 2: Emotional Weight (25%) ===
            emotional_weight = 0.0
            total_emotion_count = sum(profile.emotion_frequencies.values())
            
            for emotion, intensity in emotions.items():
                # Skip neutral and low intensity emotions
                if emotion == 'neutral' or intensity <= 0.1:
                    continue
                    
                if total_emotion_count > 0:
                    frequency = profile.emotion_frequencies.get(emotion, 0)
                    rarity = 1 - (frequency / total_emotion_count)
                else:
                    rarity = 1.0  # All emotions are new for new users
                
                emotional_weight += intensity * rarity
            
            # Normalize to 0-1 range (max theoretical value is ~3-4)
            emotional_weight = min(1.0, emotional_weight / 2.0)
            factor_details["emotional"] = emotional_weight
            
            # === Factor 3: Context Rarity (20%) ===
            tag_novelty = 0.0
            
            if tags:
                total_tag_count = sum(profile.tag_frequencies.values())
                tag_rarities = []
                
                for tag in tags:
                    if total_tag_count > 0:
                        frequency = profile.tag_frequencies.get(tag, 0)
                        rarity = 1 - (frequency / total_tag_count)
                    else:
                        rarity = 1.0  # All tags are new for new users
                    
                    tag_rarities.append(rarity)
                
                tag_novelty = sum(tag_rarities) / len(tag_rarities)
            
            factor_details["contextual"] = tag_novelty
            
            # === Factor 4: Temporal Novelty (15%) ===
            temporal_novelty = 1.0  # Default if no similar memories
            
            if tags and self._pool and not self._degraded_mode:
                try:
                    # Find last memory with similar tags
                    last_timestamp = await self._pool.fetchval(
                        """
                        SELECT created_at 
                        FROM ltm_memories
                        WHERE user_id = $1 AND semantic_tags && $2::text[]
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        user_id,
                        tags,
                        timeout=LTM_QUERY_TIMEOUT
                    )
                    
                    if last_timestamp:
                        # Calculate days passed
                        days_passed = (datetime.now(timezone.utc) - last_timestamp).days
                        # Exponential decay: after 7 days, novelty is ~63%
                        temporal_novelty = 1 - exp(-days_passed / 7)
                
                except Exception as e:
                    self.logger.warning(f"Temporal novelty calculation failed: {e}")
                    # Keep default value
            
            factor_details["temporal"] = temporal_novelty
            
            # === Factor 5: KNN Density Modifier ===
            if len(neighbors_with_distances) >= LTM_KNN_NEIGHBORS:
                # Calculate average distance to top-5
                avg_distance = sum(distances) / len(distances) if distances else 1.0
                
                if avg_distance < LTM_KNN_DENSITY_THRESHOLD:
                    # Dense region - reduce score
                    factor_details["density_modifier"] = 1 - LTM_KNN_DENSITY_PENALTY
                else:
                    factor_details["density_modifier"] = 1.0
            
            # === Calculate Final Score ===
            base_score = (
                factor_details["semantic"] * LTM_NOVELTY_SEMANTIC_WEIGHT +
                factor_details["emotional"] * LTM_NOVELTY_EMOTIONAL_WEIGHT +
                factor_details["contextual"] * LTM_NOVELTY_CONTEXT_WEIGHT +
                factor_details["temporal"] * LTM_NOVELTY_TEMPORAL_WEIGHT
            )
            
            # Apply density modifier
            final_score = base_score * factor_details["density_modifier"]
            
            # Ensure score is in valid range
            final_score = max(0.0, min(1.0, final_score))
            
            return final_score, factor_details
            
        except Exception as e:
            self.logger.error(f"Error calculating novelty score: {e}")
            # Return neutral score on error
            return 0.5, factor_details
    
    async def _generate_embedding_for_novelty(
        self, 
        user_id: str, 
        text: str, 
        emotions: Dict[str, float], 
        tags: List[str]
    ) -> Optional[np.ndarray]:
        """
        Generate embedding for novelty calculation
        
        Args:
            user_id: User ID
            text: Text to embed
            emotions: Emotion dictionary
            tags: Semantic tags
            
        Returns:
            768d numpy array or None if generation fails
        """
        try:
            # Create minimal LTMEntry for embedding generation
            messages = [Message(
                role="user",
                content=text,
                timestamp=datetime.now(timezone.utc),
                message_id="temp_novelty"
            )]
            
            temp_entry = LTMEntry(
                user_id=user_id,
                conversation_fragment=ConversationFragment(
                    messages=messages,
                    trigger_message_id="temp_novelty"
                ),
                importance_score=0.5,
                emotional_snapshot=EmotionalSnapshot.from_dict(emotions),
                dominant_emotions=["neutral"],
                emotional_intensity=0.5,
                memory_type=MemoryType.USER_RELATED,
                trigger_reason=TriggerReason.EMOTIONAL_PEAK,
                semantic_tags=tags
            )
            
            # Generate embedding using LTMActor's method
            # Note: self here is LTMActor with this mixin
            embedding = await self._generate_embedding_async(temp_entry)
            
            return embedding
            
        except Exception as e:
            self.logger.warning(f"Failed to generate embedding for novelty: {e}")
            return None
    
    async def _get_nearest_neighbors(
        self, 
        user_id: str, 
        query_vector: np.ndarray, 
        limit: int
    ) -> List[Tuple[Dict, float]]:
        """
        Get nearest neighbors with distances
        
        Args:
            user_id: User ID
            query_vector: Query embedding
            limit: Max results
            
        Returns:
            List of (entry_dict, distance) tuples
        """
        if self._degraded_mode or not self._pool:
            return []
        
        try:
            # Convert numpy array to pgvector format
            vector_str = '[' + ','.join(map(str, query_vector.tolist())) + ']'
            
            # Get entries with distances
            query = """
                SELECT *, embedding <=> $2::vector as distance
                FROM ltm_memories
                WHERE user_id = $1 AND embedding IS NOT NULL
                ORDER BY embedding <=> $2::vector
                LIMIT $3
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                vector_str,
                limit,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            # Return list of (entry_dict, distance) tuples
            results = []
            for row in rows:
                entry_dict = dict(row)
                distance = entry_dict.pop('distance', 1.0)
                results.append((entry_dict, distance))
            
            return results
            
        except Exception as e:
            self.logger.error(f"Failed to get nearest neighbors: {e}")
            return []