"""
Search mixin for LTMActor - provides vector and semantic search capabilities
"""
from typing import List, Optional, Any
import time
from datetime import datetime, timedelta, timezone
import numpy as np
from models.ltm_models import LTMEntry, MemoryType
from actors.events.ltm_events import LTMSearchCompletedEvent
from config.settings import (
    LTM_QUERY_TIMEOUT,
    LTM_SEARCH_MAX_LIMIT,
    LTM_SEARCH_DEFAULT_LIMIT,
    LTM_SEARCH_TAGS_MODE_ANY,
    LTM_SEARCH_TAGS_MODE_ALL
)


class LTMSearchMixin:
    """Mixin providing search methods for LTM"""
    
    # These attributes are available from LTMActor
    _pool: Optional[object]
    _degraded_mode: bool
    logger: object
    _event_version_manager: object
    get_actor_system: callable
    actor_id: str
    
    async def search_by_embedding(
        self, 
        query_vector: np.ndarray,
        user_id: str,
        limit: int = LTM_SEARCH_DEFAULT_LIMIT,
        offset: int = 0
    ) -> List[LTMEntry]:
        """
        Search memories by vector similarity using cosine distance
        
        Args:
            query_vector: Query embedding vector (768d)
            user_id: User ID to search memories for
            limit: Maximum number of results
            offset: Offset for pagination
            
        Returns:
            List of LTMEntry objects sorted by similarity
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            self.logger.error("Database pool not initialized")
            return []
            
        # Validate and limit parameters
        limit = min(limit, LTM_SEARCH_MAX_LIMIT)
        if query_vector.shape != (768,):
            raise ValueError(f"Query vector must be 768d, got {query_vector.shape}")
            
        start_time = time.time()
        
        try:
            # Convert numpy array to pgvector format
            vector_str = '[' + ','.join(map(str, query_vector.tolist())) + ']'
            
            # Execute vector search query
            query = """
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at,
                       embedding <=> $2::vector as distance
                FROM ltm_memories
                WHERE user_id = $1 AND embedding IS NOT NULL
                ORDER BY embedding <=> $2::vector
                LIMIT $3 OFFSET $4
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                vector_str,
                limit,
                offset,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            # Format results
            results = self._format_search_results(rows)
            
            # Update access counts for returned memories
            if results:
                memory_ids = [entry.memory_id for entry in results if entry.memory_id]
                await self._update_access_counts(memory_ids)
            
            # Generate search completed event
            search_time_ms = (time.time() - start_time) * 1000
            event = LTMSearchCompletedEvent.create(
                user_id=user_id,
                search_type='vector',
                results_count=len(results),
                search_time_ms=search_time_ms,
                query_params={'vector_dims': len(query_vector), 'limit': limit, 'offset': offset}
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return results
            
        except Exception as e:
            self.logger.error(f"Vector search failed: {str(e)}")
            return []
    
    async def get_self_related_memories(
        self, 
        user_id: str, 
        limit: int = LTM_SEARCH_DEFAULT_LIMIT,
        offset: int = 0
    ) -> List[LTMEntry]:
        """
        Get memories related to Chimera's self-identity
        
        Args:
            user_id: User ID
            limit: Maximum number of results
            offset: Offset for pagination
            
        Returns:
            List of self-related LTMEntry objects
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        limit = min(limit, LTM_SEARCH_MAX_LIMIT)
        start_time = time.time()
        
        try:
            query = """
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at
                FROM ltm_memories
                WHERE user_id = $1 AND memory_type = $2
                ORDER BY importance_score DESC, created_at DESC
                LIMIT $3 OFFSET $4
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                MemoryType.SELF_RELATED.value,
                limit,
                offset,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            results = self._format_search_results(rows)
            
            # Generate event
            search_time_ms = (time.time() - start_time) * 1000
            event = LTMSearchCompletedEvent.create(
                user_id=user_id,
                search_type='self_related',
                results_count=len(results),
                search_time_ms=search_time_ms,
                query_params={'limit': limit, 'offset': offset}
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return results
            
        except Exception as e:
            self.logger.error(f"Self-related search failed: {str(e)}")
            return []
    
    async def search_by_tags(
        self, 
        tags_list: List[str], 
        user_id: str,
        mode: str = LTM_SEARCH_TAGS_MODE_ANY,
        limit: int = LTM_SEARCH_DEFAULT_LIMIT,
        offset: int = 0
    ) -> List[LTMEntry]:
        """
        Search memories by semantic tags
        
        Args:
            tags_list: List of tags to search for
            user_id: User ID
            mode: 'any' (at least one tag) or 'all' (all tags must match)
            limit: Maximum number of results
            offset: Offset for pagination
            
        Returns:
            List of LTMEntry objects matching the tags
        """
        if self._degraded_mode:
            return []
            
        if not self._pool or not tags_list:
            return []
            
        limit = min(limit, LTM_SEARCH_MAX_LIMIT)
        start_time = time.time()
        
        try:
            # Build tags filter clause
            tags_clause = self._build_tags_filter_clause(tags_list, mode)
            
            query = f"""
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at
                FROM ltm_memories
                WHERE user_id = $1 AND {tags_clause}
                ORDER BY importance_score DESC, created_at DESC
                LIMIT $2 OFFSET $3
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                limit,
                offset,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            results = self._format_search_results(rows)
            
            # Generate event
            search_time_ms = (time.time() - start_time) * 1000
            event = LTMSearchCompletedEvent.create(
                user_id=user_id,
                search_type='tags',
                results_count=len(results),
                search_time_ms=search_time_ms,
                query_params={'tags': tags_list, 'mode': mode, 'limit': limit, 'offset': offset}
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return results
            
        except Exception as e:
            self.logger.error(f"Tags search failed: {str(e)}")
            return []
    
    async def get_recent_memories(
        self, 
        user_id: str, 
        days: int = 7,
        limit: int = LTM_SEARCH_DEFAULT_LIMIT,
        offset: int = 0
    ) -> List[LTMEntry]:
        """
        Get memories from the last N days
        
        Args:
            user_id: User ID
            days: Number of days to look back
            limit: Maximum number of results
            offset: Offset for pagination
            
        Returns:
            List of recent LTMEntry objects
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        limit = min(limit, LTM_SEARCH_MAX_LIMIT)
        start_time = time.time()
        
        try:
            # Calculate cutoff timestamp
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            
            query = """
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at
                FROM ltm_memories
                WHERE user_id = $1 AND created_at >= $2
                ORDER BY created_at DESC
                LIMIT $3 OFFSET $4
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                cutoff,
                limit,
                offset,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            results = self._format_search_results(rows)
            
            # Generate event
            search_time_ms = (time.time() - start_time) * 1000
            event = LTMSearchCompletedEvent.create(
                user_id=user_id,
                search_type='recent',
                results_count=len(results),
                search_time_ms=search_time_ms,
                query_params={'days': days, 'limit': limit, 'offset': offset}
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return results
            
        except Exception as e:
            self.logger.error(f"Recent memories search failed: {str(e)}")
            return []
    
    async def get_memories_by_importance(
        self, 
        user_id: str, 
        min_score: float = 0.8,
        limit: int = LTM_SEARCH_DEFAULT_LIMIT,
        offset: int = 0
    ) -> List[LTMEntry]:
        """
        Get memories above a certain importance threshold
        
        Args:
            user_id: User ID
            min_score: Minimum importance score (0.0-1.0)
            limit: Maximum number of results
            offset: Offset for pagination
            
        Returns:
            List of important LTMEntry objects
        """
        if self._degraded_mode:
            return []
            
        if not self._pool:
            return []
            
        limit = min(limit, LTM_SEARCH_MAX_LIMIT)
        min_score = max(0.0, min(1.0, min_score))  # Clamp to valid range
        start_time = time.time()
        
        try:
            query = """
                SELECT memory_id, user_id, conversation_fragment, importance_score,
                       emotional_snapshot, dominant_emotions, emotional_intensity,
                       memory_type, semantic_tags, self_relevance_score,
                       trigger_reason, created_at, accessed_count, last_accessed_at
                FROM ltm_memories
                WHERE user_id = $1 AND importance_score >= $2
                ORDER BY importance_score DESC, created_at DESC
                LIMIT $3 OFFSET $4
            """
            
            rows = await self._pool.fetch(
                query,
                user_id,
                min_score,
                limit,
                offset,
                timeout=LTM_QUERY_TIMEOUT
            )
            
            results = self._format_search_results(rows)
            
            # Generate event
            search_time_ms = (time.time() - start_time) * 1000
            event = LTMSearchCompletedEvent.create(
                user_id=user_id,
                search_type='importance',
                results_count=len(results),
                search_time_ms=search_time_ms,
                query_params={'min_score': min_score, 'limit': limit, 'offset': offset}
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            return results
            
        except Exception as e:
            self.logger.error(f"Importance search failed: {str(e)}")
            return []
    
    def _format_search_results(self, rows: List[Any]) -> List[LTMEntry]:
        """
        Format database rows into LTMEntry objects
        
        Args:
            rows: asyncpg Record objects
            
        Returns:
            List of LTMEntry objects
        """
        results = []
        
        for row in rows:
            try:
                # Convert row to dict
                entry_dict = dict(row)
                
                # Parse JSON fields if they're strings
                if isinstance(entry_dict.get('conversation_fragment'), str):
                    import json
                    entry_dict['conversation_fragment'] = json.loads(entry_dict['conversation_fragment'])
                
                if isinstance(entry_dict.get('emotional_snapshot'), str):
                    import json
                    entry_dict['emotional_snapshot'] = json.loads(entry_dict['emotional_snapshot'])
                
                # Remove distance field if present (from vector search)
                entry_dict.pop('distance', None)
                
                # Create LTMEntry
                entry = LTMEntry(**entry_dict)
                results.append(entry)
                
            except Exception as e:
                self.logger.error(f"Failed to format result: {str(e)}")
                continue
        
        return results
    
    def _build_tags_filter_clause(self, tags: List[str], mode: str) -> str:
        """
        Build SQL clause for tags filtering
        
        Args:
            tags: List of tags
            mode: 'any' or 'all'
            
        Returns:
            SQL WHERE clause fragment
        """
        # Convert tags to SQL array literal
        tags_array = "ARRAY[" + ",".join(f"'{tag}'" for tag in tags) + "]::text[]"
        
        if mode == LTM_SEARCH_TAGS_MODE_ALL:
            # All tags must be present
            return f"semantic_tags @> {tags_array}"
        else:
            # At least one tag must match (default)
            return f"semantic_tags && {tags_array}"
    
    async def _update_access_counts(self, memory_ids: List[Any]) -> None:
        """
        Update access counts for retrieved memories
        
        Args:
            memory_ids: List of memory UUIDs
        """
        if not memory_ids or not self._pool:
            return
            
        try:
            # Batch update access counts
            query = """
                UPDATE ltm_memories
                SET 
                    accessed_count = accessed_count + 1,
                    last_accessed_at = CURRENT_TIMESTAMP
                WHERE memory_id = ANY($1::uuid[])
            """
            
            await self._pool.execute(
                query,
                memory_ids,
                timeout=LTM_QUERY_TIMEOUT
            )
            
        except Exception as e:
            self.logger.warning(f"Failed to update access counts: {str(e)}")
            # Non-critical error, don't propagate