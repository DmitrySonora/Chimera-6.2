"""
Embedding generation mixin for LTMActor - handles vector embeddings
"""
from typing import Optional
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from models.ltm_models import LTMEntry
from models.embedding_generator import EmbeddingGenerator
from config.settings import (
    LTM_EMBEDDING_THREAD_POOL_SIZE
)


class LTMEmbeddingMixin:
    """Mixin providing embedding generation methods for LTMActor"""
    
    # These attributes are available from LTMActor
    logger: object
    
    # These will be set by this mixin but declared in main class
    _embedding_generator: Optional[EmbeddingGenerator]
    _embedding_thread_pool: Optional[ThreadPoolExecutor]
    
    async def _initialize_embeddings(self) -> None:
        """Initialize embedding generator and thread pool"""
        try:
            self._embedding_generator = EmbeddingGenerator()
            self._embedding_thread_pool = ThreadPoolExecutor(
                max_workers=LTM_EMBEDDING_THREAD_POOL_SIZE,
                thread_name_prefix="embedding-generator"
            )
            self.logger.info("Embedding generator initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize embeddings: {e}")
            # Continue working without embeddings
            self._embedding_generator = None
            self._embedding_thread_pool = None
    
    async def _shutdown_embeddings(self) -> None:
        """Shutdown thread pool for embeddings"""
        if self._embedding_thread_pool:
            self._embedding_thread_pool.shutdown(wait=True)
            self.logger.debug("Embedding thread pool shut down")
    
    async def _generate_embedding_async(self, ltm_entry: LTMEntry) -> Optional[np.ndarray]:
        """Асинхронная генерация embedding"""
        if not self._embedding_generator or not self._embedding_thread_pool:
            return None
            
        loop = asyncio.get_event_loop()
        
        try:
            # Извлекаем текст из conversation_fragment
            text_parts = []
            for msg in ltm_entry.conversation_fragment.messages:
                text_parts.append(msg.content)
            text = " ".join(text_parts)
            
            # Генерируем в отдельном потоке
            embedding = await loop.run_in_executor(
                self._embedding_thread_pool,
                self._embedding_generator.generate_composite_embedding,
                text,
                ltm_entry.emotional_snapshot.to_dict(),
                ltm_entry.created_at or datetime.now(),
                ltm_entry.semantic_tags,
                ltm_entry.memory_type.value
            )
            
            return embedding
            
        except Exception as e:
            self.logger.warning(f"Failed to generate embedding: {e}")
            return None