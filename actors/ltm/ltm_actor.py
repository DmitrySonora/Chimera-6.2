"""
LTMActor - актор для управления долговременной памятью (LTM).
Сохраняет важные воспоминания с эмоциональным контекстом.
"""
from typing import Optional, Any, Dict
import asyncio
import json
import time
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from actors.base_actor import BaseActor
from actors.messages import ActorMessage, MESSAGE_TYPES
from config.settings import (
    LTM_QUERY_TIMEOUT,
    LTM_SCHEMA_CHECK_TIMEOUT,
)
from database.connection import db_connection
from utils.monitoring import measure_latency
from utils.event_utils import EventVersionManager
from models.ltm_models import LTMEntry
from actors.events.ltm_events import (
    LTMSavedEvent, 
    LTMDegradedModeEvent
)
from .search_mixin import LTMSearchMixin
from .analytics_mixin import LTMAnalyticsMixin
from .novelty_mixin import LTMNoveltyMixin
from .message_handling_mixin import LTMMessageHandlingMixin
from .embedding_mixin import LTMEmbeddingMixin
from .validation_mixin import LTMValidationMixin
from .metrics_mixin import LTMMetricsMixin
from models.embedding_generator import EmbeddingGenerator


class LTMActor(BaseActor, LTMSearchMixin, LTMAnalyticsMixin, LTMNoveltyMixin, LTMMessageHandlingMixin, LTMEmbeddingMixin, LTMValidationMixin, LTMMetricsMixin):
    """
    Актор для управления долговременной памятью (LTM).
    Сохраняет важные воспоминания с эмоциональным контекстом.
    """
    
    def __init__(self):
        super().__init__("ltm", "LTM")
        self._pool = None
        self._degraded_mode = False
        self._event_version_manager = EventVersionManager()
        
        # Embedding generator
        self._embedding_generator: Optional[EmbeddingGenerator] = None
        self._embedding_thread_pool: Optional[ThreadPoolExecutor] = None
        
        # Метрики (инициализируются в _initialize_metrics)
        self._metrics: Dict[str, int] = {}
        self._metrics_task: Optional[asyncio.Task] = None
        
    async def initialize(self) -> None:
        """Инициализация актора и проверка схемы БД"""
        try:
            # Проверяем, нужно ли подключаться
            if not db_connection._is_connected:
                await db_connection.connect()
            
            # Получаем пул подключений
            self._pool = db_connection.get_pool()
            
            # Проверяем существование таблицы и индексов
            await self._verify_schema()
            
            # Инициализация метрик
            self._initialize_metrics()
            self._metrics['initialized'] = True
            
            # Инициализация генератора embeddings
            await self._initialize_embeddings()
            
            self.logger.info("LTMActor initialized successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize LTMActor: {str(e)}")
            self._degraded_mode = True
            self._metrics['degraded_mode_entries'] += 1
            self._increment_metric('db_errors')
            
            # Генерируем событие о переходе в degraded mode
            event = LTMDegradedModeEvent.create(
                reason="initialization_failed",
                details=str(e)
            )
            await self._event_version_manager.append_event(
                event,
                self.get_actor_system()
            )
            
            self.logger.warning("LTMActor entering degraded mode - will work without persistence")
    
    async def shutdown(self) -> None:
        """Освобождение ресурсов"""
        # Останавливаем метрики
        await self._shutdown_metrics()
        
        # Закрываем thread pool для embeddings
        await self._shutdown_embeddings()
        
        self.logger.info("LTMActor shutdown completed")
    
    @measure_latency
    async def handle_message(self, message: ActorMessage) -> Optional[ActorMessage]:
        """Маршрутизация сообщений"""
        
        handlers = {
            MESSAGE_TYPES['SAVE_TO_LTM']: ('save_memory_count', self._handle_save_memory),
            MESSAGE_TYPES['GET_LTM_MEMORY']: ('get_memory_count', self._handle_get_memory),
            MESSAGE_TYPES['DELETE_LTM_MEMORY']: ('delete_memory_count', self._handle_delete_memory),
            MESSAGE_TYPES['EVALUATE_FOR_LTM']: ('evaluation_count', self._handle_ltm_evaluation),
        }
        
        handler_info = handlers.get(message.message_type)
        if handler_info:
            metric_name, handler = handler_info
            self._increment_metric(metric_name)
            await handler(message)
        else:
            self._increment_metric('unknown_message_count')
            self.logger.warning(f"Unknown message type: {message.message_type}")
        
        return None
    
    async def _verify_schema(self) -> None:
        """Проверка существования таблицы и индексов"""
        try:
            if self._pool is None:
                raise RuntimeError("Database pool not initialized")
                
            # Проверяем существование таблицы
            query = """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'ltm_memories'
                )
            """
            table_exists = await self._pool.fetchval(
                query, 
                timeout=LTM_SCHEMA_CHECK_TIMEOUT
            )
            
            if not table_exists:
                raise RuntimeError("Table ltm_memories does not exist. Run migrations first.")
            
            # Проверяем индексы
            index_query = """
                SELECT indexname 
                FROM pg_indexes 
                WHERE tablename = 'ltm_memories'
            """
            indexes = await self._pool.fetch(
                index_query, 
                timeout=LTM_SCHEMA_CHECK_TIMEOUT
            )
            
            required_indexes = {
                'idx_ltm_user_timestamp',
                'idx_ltm_dominant_emotions', 
                'idx_ltm_semantic_tags',
                'idx_ltm_memory_type',
                'idx_ltm_importance_timestamp',
                'idx_ltm_trigger_reason',
                'idx_ltm_accessed'
            }
            
            existing_indexes = {row['indexname'] for row in indexes}
            missing_indexes = required_indexes - existing_indexes
            
            if missing_indexes:
                self.logger.warning(f"Missing indexes: {missing_indexes}")
            
            self.logger.debug("Schema verification completed successfully")
            
        except Exception as e:
            self.logger.error(f"Schema verification failed: {str(e)}")
            raise
    
    @measure_latency
    async def save_memory(self, ltm_entry: LTMEntry) -> UUID:
        """
        Сохранить воспоминание в долговременную память.
        
        Args:
            ltm_entry: Валидированная запись LTM
            
        Returns:
            UUID сохраненного воспоминания
            
        Raises:
            Exception: При ошибке сохранения
        """
        if self._pool is None:
            raise RuntimeError("Database pool not initialized")
            
        if self._degraded_mode:
            raise RuntimeError("LTMActor is in degraded mode")
            
        try:
            # Валидация уже прошла через Pydantic
            # Дополнительная валидация эмоционального снимка
            self._validate_emotional_snapshot(ltm_entry.emotional_snapshot)
            
            # Если не указаны семантические теги - извлекаем базовые
            if not ltm_entry.semantic_tags:
                ltm_entry.semantic_tags = self._extract_semantic_tags(
                    ltm_entry.conversation_fragment
                )
            
            # Генерируем embedding если доступен генератор
            embedding = None
            if self._embedding_generator:
                try:
                    start_time = time.time()
                    embedding = await self._generate_embedding_async(ltm_entry)
                    if embedding is not None:
                        self.logger.debug(
                            f"Generated embedding in {time.time() - start_time:.2f}s"
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to generate embedding: {e}")
                    # Продолжаем без embedding
            
            # Подготавливаем данные для БД
            db_data = ltm_entry.to_db_dict()
            
            async with self._pool.acquire() as conn:
                # Выполняем вставку и получаем memory_id
                result = await conn.fetchrow(
                    """
                    INSERT INTO ltm_memories (
                        user_id, conversation_fragment, importance_score,
                        emotional_snapshot, dominant_emotions, emotional_intensity,
                        memory_type, semantic_tags, self_relevance_score,
                        trigger_reason, embedding
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::vector
                    ) RETURNING memory_id
                    """,
                    db_data['user_id'],
                    json.dumps(db_data['conversation_fragment']),
                    db_data['importance_score'],
                    json.dumps(db_data['emotional_snapshot']),
                    db_data['dominant_emotions'],
                    db_data['emotional_intensity'],
                    db_data['memory_type'],
                    db_data['semantic_tags'],
                    db_data['self_relevance_score'],
                    db_data['trigger_reason'],
                    '[' + ','.join(map(str, embedding.tolist())) + ']' if embedding is not None else None,
                    timeout=LTM_QUERY_TIMEOUT
                )
                
                memory_id = result['memory_id']
            
            # Создаем событие успешного сохранения
            event = LTMSavedEvent.create(
                memory_id=str(memory_id),
                user_id=ltm_entry.user_id,
                memory_type=ltm_entry.memory_type.value,
                importance_score=ltm_entry.importance_score,
                trigger_reason=ltm_entry.trigger_reason.value,
                emotional_intensity=ltm_entry.emotional_intensity
            )
            await self._event_version_manager.append_event(
                event,
                self.get_actor_system()
            )
            
            self.logger.info(
                f"Saved LTM memory {memory_id} for user {ltm_entry.user_id}: "
                f"type={ltm_entry.memory_type.value}, "
                f"importance={ltm_entry.importance_score:.2f}, "
                f"reason={ltm_entry.trigger_reason.value}"
            )
            
            return memory_id
            
        except Exception as e:
            self.logger.error(f"Failed to save memory: {str(e)}")
            self._increment_metric('db_errors')
            raise
    
    async def _evaluate_importance(self, payload: Dict[str, Any]) -> tuple[bool, float]:
        """
        Оценить важность для сохранения в LTM используя многофакторную оценку
        
        Args:
            payload: Данные для оценки
            
        Returns:
            (should_save, novelty_score)
        """
        import math
        from datetime import datetime, timezone
        from actors.events.ltm_events import (
            NoveltyCalculatedEvent, 
            CalibrationProgressEvent,
            MemoryRejectedEvent
        )
        from config.settings import (
            LTM_COLD_START_BUFFER_SIZE,
            LTM_COLD_START_MIN_THRESHOLD,
            LTM_MATURITY_SIGMOID_RATE
        )
        
        try:
            # Извлекаем необходимые данные из payload
            user_id = payload.get('user_id', '')
            text = payload.get('user_text', '') + ' ' + payload.get('bot_response', '')
            emotions = payload.get('emotions', {})
            
            # Извлекаем теги из сообщений
            messages = payload.get('messages', [])
            tags = []
            
            if messages:
                from models.ltm_models import ConversationFragment, Message
                
                fragment_messages = []
                for msg in messages:
                    fragment_messages.append(Message(
                        role=msg.get('role', 'user'),
                        content=msg.get('content', ''),
                        timestamp=msg.get('timestamp', datetime.now(timezone.utc)),
                        message_id=msg.get('message_id', 'unknown')
                    ))
                
                conversation_fragment = ConversationFragment(
                    messages=fragment_messages,
                    trigger_message_id=fragment_messages[-1].message_id if fragment_messages else 'unknown'
                )
                
                tags = self._extract_semantic_tags(conversation_fragment)
            
            # Получаем профиль пользователя
            profile = await self._get_or_create_profile(user_id)
            
            # Вызываем многофакторную оценку новизны
            novelty_score, factor_details = await self.calculate_novelty_score(
                user_id, text, emotions, tags, profile
            )
            
            # Проверка холодного старта
            if profile.total_messages < LTM_COLD_START_BUFFER_SIZE:
                # Инкрементируем метрику
                self._increment_metric('calibration_skip_count')
                
                # Генерируем событие калибровки
                calibration_event = CalibrationProgressEvent.create(
                    user_id=user_id,
                    messages_processed=profile.total_messages,
                    calibration_complete=False
                )
                await self._event_version_manager.append_event(
                    calibration_event, self.get_actor_system()
                )
                
                # Генерируем событие оценки новизны (saved=False)
                novelty_event = NoveltyCalculatedEvent.create(
                    user_id=user_id,
                    novelty_score=novelty_score,
                    factor_details=factor_details,
                    saved=False
                )
                await self._event_version_manager.append_event(
                    novelty_event, self.get_actor_system()
                )
                
                self.logger.debug(
                    f"Cold start calibration for {user_id}: "
                    f"messages={profile.total_messages}/{LTM_COLD_START_BUFFER_SIZE}"
                )
                
                return False, novelty_score
            
            # Расчет динамического порога
            base_threshold = max(
                profile.current_percentile_90 * 0.9,
                LTM_COLD_START_MIN_THRESHOLD
            )
            
            # Сигмоидное сглаживание по времени
            days_since_start = (datetime.now(timezone.utc) - profile.created_at).days
            maturity_factor = 1 / (1 + math.exp(-LTM_MATURITY_SIGMOID_RATE * (days_since_start - 30)))
            # Используем инвертированный maturity для порога
            # Молодые профили = высокий порог, зрелые = низкий
            inverted_maturity = 1 - maturity_factor
            final_threshold = base_threshold + (1 - base_threshold) * inverted_maturity * 0.5
            
            # Принятие решения
            should_save = novelty_score > final_threshold
            
            # Генерируем событие оценки новизны
            novelty_event = NoveltyCalculatedEvent.create(
                user_id=user_id,
                novelty_score=novelty_score,
                factor_details=factor_details,
                saved=should_save
            )
            await self._event_version_manager.append_event(
                novelty_event, self.get_actor_system()
            )
            
            # Если отклонено - генерируем событие отклонения
            if not should_save and novelty_score > 0.5:  # Значимые но отклоненные
                self._increment_metric('novelty_rejection_count')
                
                rejection_event = MemoryRejectedEvent.create(
                    user_id=user_id,
                    novelty_score=novelty_score,
                    threshold=final_threshold,
                    reason=f"below_dynamic_threshold_{final_threshold:.3f}"
                )
                await self._event_version_manager.append_event(
                    rejection_event, self.get_actor_system()
                )
            
            self.logger.debug(
                f"Novelty evaluation for {user_id}: score={novelty_score:.3f}, "
                f"threshold={final_threshold:.3f}, maturity={maturity_factor:.3f}, "
                f"save={should_save}"
            )
            
            return should_save, novelty_score
            
        except Exception as e:
            self.logger.error(f"Error in _evaluate_importance: {e}")
            # При ошибке возвращаем безопасные значения
            return False, 0.0