from typing import Dict, Any
from datetime import datetime
import asyncio
import uuid
from actors.messages import ActorMessage, MESSAGE_TYPES
from actors.events import BaseEvent
from config.settings import STM_CONTEXT_REQUEST_TIMEOUT, AUTH_CHECK_TIMEOUT, AUTH_FALLBACK_TO_DEMO, MODE_HISTORY_SIZE, STM_CONTEXT_SIZE_FOR_GENERATION
from config.prompts import PROMPT_CONFIG

class RequestHandlingMixin:
    async def _cleanup_pending_requests_loop(self) -> None:
        """Периодическая очистка зависших запросов"""
        while self.is_running:
            try:
                await asyncio.sleep(10)  # Проверка каждые 10 секунд
                await self._cleanup_expired_requests()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in cleanup loop: {str(e)}")
    
    async def _cleanup_expired_requests(self) -> None:
        """Очистка запросов старше таймаута"""
        now = datetime.now()
        expired = []
        
        for request_id, data in self._pending_requests.items():
            if (now - data['timestamp']).total_seconds() > STM_CONTEXT_REQUEST_TIMEOUT:
                expired.append(request_id)
        
        for request_id in expired:
            pending = self._pending_requests.pop(request_id)
            self.logger.warning(
                f"Context request timeout for user {pending['user_id']}, "
                f"generating without historical context"
            )
            
            # Генерируем без исторического контекста как fallback
            generate_msg = ActorMessage.create(
                sender_id=self.actor_id,
                message_type=MESSAGE_TYPES['GENERATE_RESPONSE'],
                payload={
                    'user_id': pending['user_id'],
                    'chat_id': pending['chat_id'],
                    'text': pending['text'],
                    'include_prompt': pending['include_prompt'],
                    'message_count': pending['message_count'],
                    'session_data': pending['session_data'],
                    'mode': pending['mode'],
                    'mode_confidence': pending['mode_confidence'],
                    'historical_context': []  # Пустой контекст при таймауте
                }
            )
            
            if self.get_actor_system():
                await self.get_actor_system().send_message("generation", generate_msg)
        
        # Очистка зависших limit запросов
        
        expired_limits = []
        for request_id, data in self._pending_limits.items():
            if (now - data['timestamp']).total_seconds() > AUTH_CHECK_TIMEOUT:
                expired_limits.append(request_id)
        
        for request_id in expired_limits:
            pending = self._pending_limits.pop(request_id)
            self.logger.warning(
                f"Limit check timeout for user {pending['user_id']}, "
                f"continuing with demo mode"
            )
            
            # Если AUTH_FALLBACK_TO_DEMO включен - продолжить обработку
            if AUTH_FALLBACK_TO_DEMO:
                await self._continue_message_processing(pending)
    
    async def _continue_message_processing(self, pending: Dict[str, Any]) -> None:
        """Продолжить обработку после проверки лимитов"""
        # Восстановить контекст
        user_id = pending['user_id']
        text = pending['text']
        chat_id = pending['chat_id']
        # username = pending['username']
        session = pending['session']
        # message = pending['message']
        
        self.logger.debug(f"Continuing message processing for user {user_id} after limit check")
        
        # Анализ эмоций (fire-and-forget подход)
        if self.get_actor_system():
            analyze_msg = ActorMessage.create(
                sender_id=self.actor_id,
                message_type=MESSAGE_TYPES['ANALYZE_EMOTION'],
                payload={
                    'user_id': user_id,
                    'text': text
                },
                reply_to=self.actor_id
            )
            await self.get_actor_system().send_message("perception", analyze_msg)
            self.logger.info("Sent ANALYZE_EMOTION")
        
        # Определяем режим общения
        new_mode, confidence = self._determine_generation_mode(text, session)
        
        # Проверка изменения режима
        mode_changed = False
        if new_mode != session.current_mode or session.current_mode is None:
            session.last_mode_change = datetime.now()
            session.current_mode = new_mode
            mode_changed = True
            
        # Всегда обновляем уверенность
        session.mode_confidence = confidence
        
        # Обновляем историю режимов
        session.mode_history.append(new_mode)
        if len(session.mode_history) > MODE_HISTORY_SIZE:
            session.mode_history.pop(0)
        
        # Логирование для отладки
        self.logger.info(
            f"Mode detection: {new_mode} "
            f"(confidence: {confidence:.2f}, changed: {mode_changed})"
        )
        
        # Создаем событие если режим изменился
        if mode_changed:
            mode_event = BaseEvent.create(
                stream_id=f"user_{user_id}",
                event_type="ModeDetectedEvent",
                data={
                    "user_id": user_id,
                    "mode": new_mode,
                    "confidence": confidence,
                    "previous_mode": session.mode_history[-2] if len(session.mode_history) > 1 else None,
                    "detection_details": getattr(self, '_last_detection_details', {}),
                    "timestamp": datetime.now().isoformat()
                }
            )
            await self._append_event(mode_event)
        
        # Обновляем счетчики
        session.message_count += 1
        session.last_activity = datetime.now()
        
        # Определяем необходимость системного промпта
        include_prompt = self._should_include_prompt(session)
        
        # Логируем решение о промпте
        if include_prompt:
            prompt_event = BaseEvent.create(
                stream_id=f"user_{user_id}",
                event_type="PromptInclusionEvent",
                data={
                    "user_id": user_id,
                    "message_count": session.message_count,
                    "strategy": PROMPT_CONFIG["prompt_strategy"],
                    "reason": self._get_prompt_reason(session)
                }
            )
            await self._append_event(prompt_event)
        
        # Сохраняем контекст генерации для последующего использования
        request_id = str(uuid.uuid4())
        self._pending_requests[request_id] = {
            'user_id': user_id,
            'chat_id': chat_id,
            'text': text,
            'include_prompt': include_prompt,
            'message_count': session.message_count,
            'session_data': {
                'username': session.username,
                'created_at': session.created_at.isoformat()
            },
            'mode': session.current_mode,
            'mode_confidence': session.mode_confidence,
            'timestamp': datetime.now()
        }
        
        # Запрашиваем исторический контекст из MemoryActor
        get_context_msg = ActorMessage.create(
            sender_id=self.actor_id,
            message_type=MESSAGE_TYPES['GET_CONTEXT'],
            payload={
                'user_id': user_id,
                'request_id': request_id,
                'limit': STM_CONTEXT_SIZE_FOR_GENERATION,
                'format_type': 'structured'  # Для DeepSeek API
            },
            reply_to=self.actor_id  # Ответ нужен нам
        )
        
        await self.get_actor_system().send_message("memory", get_context_msg)
        self.logger.info(f"Requested context for user {user_id}")