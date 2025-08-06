"""
Message handling mixin for LTMActor - processes incoming messages
"""
from typing import Optional
from datetime import datetime, timezone
import numpy as np
from actors.messages import ActorMessage, MESSAGE_TYPES
from models.ltm_models import LTMEntry, create_ltm_entry
from actors.events.ltm_events import (
    LTMErrorEvent, 
    LTMSearchErrorEvent,
    ImportanceCalculatedEvent
)
from config.settings import (
    LTM_SEARCH_DEFAULT_LIMIT,
    LTM_SEARCH_TAGS_MODE_ANY,
    LTM_SEARCH_RECENT_DAYS_DEFAULT,
    LTM_SEARCH_MIN_IMPORTANCE_DEFAULT,
    LTM_EMOTIONAL_THRESHOLD,
)


class LTMMessageHandlingMixin:
    """Mixin providing message handling methods for LTMActor"""
    
    # These attributes are available from LTMActor
    _pool: Optional[object]
    _degraded_mode: bool
    logger: object
    actor_id: str
    _event_version_manager: object
    get_actor_system: callable
    
    # Methods from other mixins or main class
    save_memory: callable  # From main class
    search_by_embedding: callable  # From search_mixin
    search_by_tags: callable  # From search_mixin
    get_self_related_memories: callable  # From search_mixin
    get_recent_memories: callable  # From search_mixin
    get_memories_by_importance: callable  # From search_mixin
    _increment_metric: callable  # Will be from metrics_mixin
    _get_or_create_profile: callable  # From novelty_mixin
    _extract_semantic_tags: callable  # Will be from validation_mixin
    _update_profile_statistics: callable  # From novelty_mixin
    _evaluate_importance: callable  # Remains in main class
    
    async def _handle_save_memory(self, message: ActorMessage) -> None:
        """Обработчик сохранения в долговременную память"""
        if self._degraded_mode:
            self.logger.debug(
                f"SAVE_TO_LTM in degraded mode for user {message.payload.get('user_id')}"
            )
            return
        
        try:
            # Извлекаем LTMEntry из payload
            ltm_data = message.payload.get('ltm_entry')
            if not ltm_data:
                raise ValueError("Missing ltm_entry in payload")
            
            # Создаем LTMEntry из данных
            if isinstance(ltm_data, dict):
                ltm_entry = LTMEntry(**ltm_data)
            elif isinstance(ltm_data, LTMEntry):
                ltm_entry = ltm_data
            else:
                raise ValueError(f"Invalid ltm_entry type: {type(ltm_data)}")
            
            # Сохраняем в БД
            memory_id = await self.save_memory(ltm_entry)
            
            # Отправляем ответ если указан получатель
            if message.reply_to and self.get_actor_system():
                response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['LTM_RESPONSE'],
                    payload={
                        'success': True,
                        'memory_id': str(memory_id),
                        'user_id': ltm_entry.user_id,
                        'operation': 'save'
                    }
                )
                await self.get_actor_system().send_message(message.reply_to, response)
            
        except Exception as e:
            self.logger.error(f"Failed to save LTM: {str(e)}")
            self._increment_metric('validation_errors')
            
            # Генерируем событие об ошибке
            event = LTMErrorEvent.create(
                user_id=message.payload.get('user_id', 'unknown'),
                operation='save',
                error_type=type(e).__name__,
                error_message=str(e)
            )
            await self._event_version_manager.append_event(
                event,
                self.get_actor_system()
            )
    
    async def _handle_get_memory(self, message: ActorMessage) -> None:
        """Обработчик получения памяти"""
        if self._degraded_mode:
            self.logger.debug("GET_LTM_MEMORY in degraded mode")
            return
        
        try:
            # Извлекаем параметры поиска
            search_type = message.payload.get('search_type', 'recent')
            user_id = message.payload.get('user_id')
            limit = message.payload.get('limit', LTM_SEARCH_DEFAULT_LIMIT)
            offset = message.payload.get('offset', 0)
            
            if not user_id:
                raise ValueError("user_id is required for memory search")
            
            # Вызываем соответствующий метод поиска
            results = []
            search_params = {}
            
            if search_type == 'vector':
                query_vector = message.payload.get('query_vector')
                if query_vector:
                    results = await self.search_by_embedding(
                        query_vector=np.array(query_vector),
                        user_id=user_id,
                        limit=limit,
                        offset=offset
                    )
                    search_params = {'vector_dims': len(query_vector)}
                    
            elif search_type == 'tags':
                tags = message.payload.get('tags', [])
                mode = message.payload.get('mode', LTM_SEARCH_TAGS_MODE_ANY)
                results = await self.search_by_tags(
                    tags_list=tags,
                    user_id=user_id,
                    mode=mode,
                    limit=limit,
                    offset=offset
                )
                search_params = {'tags': tags, 'mode': mode}
                
            elif search_type == 'self_related':
                results = await self.get_self_related_memories(
                    user_id=user_id,
                    limit=limit,
                    offset=offset
                )
                
            elif search_type == 'recent':
                days = message.payload.get('days', LTM_SEARCH_RECENT_DAYS_DEFAULT)
                results = await self.get_recent_memories(
                    user_id=user_id,
                    days=days,
                    limit=limit,
                    offset=offset
                )
                search_params = {'days': days}
                
            elif search_type == 'importance':
                min_score = message.payload.get('min_score', LTM_SEARCH_MIN_IMPORTANCE_DEFAULT)
                results = await self.get_memories_by_importance(
                    user_id=user_id,
                    min_score=min_score,
                    limit=limit,
                    offset=offset
                )
                search_params = {'min_score': min_score}
                
            else:
                raise ValueError(f"Unknown search type: {search_type}")
            
            # Отправляем ответ
            if message.reply_to and self.get_actor_system():
                response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['LTM_RESPONSE'],
                    payload={
                        'success': True,
                        'operation': 'search',
                        'search_type': search_type,
                        'results': [entry.model_dump() for entry in results],
                        'count': len(results),
                        'user_id': user_id,
                        'search_params': search_params
                    }
                )
                await self.get_actor_system().send_message(message.reply_to, response)
                
        except Exception as e:
            self.logger.error(f"Failed to search LTM: {str(e)}")
            self._increment_metric('validation_errors')
            
            # Генерируем событие об ошибке
            event = LTMSearchErrorEvent.create(
                user_id=message.payload.get('user_id', 'unknown'),
                search_type=message.payload.get('search_type', 'unknown'),
                error_type=type(e).__name__,
                error_message=str(e)
            )
            await self._event_version_manager.append_event(
                event,
                self.get_actor_system()
            )
            
            # Отправляем ответ об ошибке
            if message.reply_to and self.get_actor_system():
                error_response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['LTM_RESPONSE'],
                    payload={
                        'success': False,
                        'operation': 'search',
                        'error': str(e),
                        'user_id': message.payload.get('user_id')
                    }
                )
                await self.get_actor_system().send_message(message.reply_to, error_response)
    
    async def _handle_delete_memory(self, message: ActorMessage) -> None:
        """Обработчик удаления памяти (заглушка для этапа 6.1.2)"""
        if self._degraded_mode:
            self.logger.debug("DELETE_LTM_MEMORY in degraded mode")
            return
        
        # TODO: Реализация позже
        self.logger.debug("DELETE_LTM_MEMORY handler called (stub)")
    
    async def _handle_ltm_evaluation(self, message: ActorMessage) -> None:
        """
        Обработчик оценки для сохранения в LTM
        
        Args:
            message: Сообщение с данными для оценки
        """
        if self._degraded_mode:
            self.logger.debug(
                f"EVALUATE_FOR_LTM in degraded mode for user {message.payload.get('user_id')}"
            )
            return
            
        try:
            payload = message.payload
            user_id = payload.get('user_id')
            
            if not user_id:
                raise ValueError("Missing user_id in EVALUATE_FOR_LTM payload")
            
            # Получаем или создаем профиль пользователя
            profile = await self._get_or_create_profile(user_id)
            
            # Извлекаем эмоции из payload
            emotions = payload.get('emotions', {})
            
            # Извлекаем теги из текста сообщений
            messages = payload.get('messages', [])
            
            if messages:
                # Создаем временный conversation_fragment для извлечения тегов
                from models.ltm_models import ConversationFragment, Message
                
                # Преобразуем сообщения в формат для ConversationFragment
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
            else:
                tags = []
            
            # Выполняем оценку важности
            should_save, novelty_score = await self._evaluate_importance(payload)
            
            # Обновляем статистику профиля с полученным novelty_score
            await self._update_profile_statistics(profile, emotions, tags, novelty_score)
            
            # Создаем событие оценки (всегда, даже если не сохраняем)
            importance_event = ImportanceCalculatedEvent.create(
                user_id=user_id,
                importance_score=novelty_score,
                saved=should_save,
                threshold=LTM_EMOTIONAL_THRESHOLD,
                trigger_reason=payload.get('trigger_reason') if should_save else None
            )
            await self._event_version_manager.append_event(
                importance_event,
                self.get_actor_system()
            )
            
            # Если оценка положительная - сохраняем
            if should_save:
                # Создаем LTMEntry используя helper
                ltm_entry = create_ltm_entry(
                    user_id=user_id,
                    messages=payload.get('messages', []),
                    emotions=payload.get('emotions', {}),
                    importance_score=novelty_score,
                    memory_type=payload.get('memory_type', 'user_related'),
                    trigger_reason=payload.get('trigger_reason', 'emotional_peak'),
                    semantic_tags=None,  # Будут извлечены автоматически
                    self_relevance_score=0.8 if payload.get('memory_type') == 'self_related' else None
                )
                
                # Сохраняем через существующий метод
                memory_id = await self.save_memory(ltm_entry)
                self._increment_metric('evaluation_saved_count')
                
                self.logger.info(
                    f"LTM evaluation: saved memory {memory_id} for user {user_id} "
                    f"(importance: {novelty_score:.2f})"
                )
            else:
                self.logger.debug(
                    f"LTM evaluation: skipped for user {user_id} "
                    f"(importance: {novelty_score:.2f} < {LTM_EMOTIONAL_THRESHOLD})"
                )
                
        except Exception as e:
            self.logger.error(f"Failed to evaluate for LTM: {str(e)}")
            self._increment_metric('validation_errors')
            
            # Генерируем событие об ошибке
            event = LTMErrorEvent.create(
                user_id=payload.get('user_id', 'unknown'),
                operation='evaluate',
                error_type=type(e).__name__,
                error_message=str(e)
            )
            await self._event_version_manager.append_event(
                event,
                self.get_actor_system()
            )