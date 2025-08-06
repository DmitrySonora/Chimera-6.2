from typing import Dict, Optional, List, Any
from pydantic import BaseModel, Field, field_validator, ConfigDict
from datetime import datetime
import asyncio
import uuid
from actors.base_actor import BaseActor
from actors.messages import ActorMessage, MESSAGE_TYPES
from actors.events import BaseEvent, EmotionDetectedEvent
from actors.user_session.mode_detection import ModeDetectionMixin
from actors.user_session.prompt_management import PromptManagementMixin
from actors.user_session.request_handling import RequestHandlingMixin
from actors.user_session.ltm_coordination import LTMCoordinationMixin
from config.prompts import PROMPT_CONFIG
from config.settings import STM_CONTEXT_SIZE_FOR_GENERATION, EMOTION_EMOJI_MAP, DAILY_MESSAGE_LIMIT
from utils.monitoring import measure_latency
from utils.event_utils import EventVersionManager

class UserSession(BaseModel):
    """Данные сессии пользователя"""
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True
    )
    
    user_id: str
    username: Optional[str] = None
    message_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    last_activity: datetime = Field(default_factory=datetime.now)
    cache_metrics: List[float] = Field(default_factory=list)
    
    # Поля для режимов общения
    current_mode: str = 'talk'
    mode_confidence: float = 0.0
    mode_history: List[str] = Field(default_factory=list)
    last_mode_change: Optional[datetime] = None
    
    # Расширяемость для будущего
    emotional_state: Optional[Any] = None
    style_vector: Optional[Any] = None
    memory_buffer: List[Any] = Field(default_factory=list)
    
    # Поля для эмоций
    last_emotion_vector: Optional[Dict[str, float]] = None
    last_dominant_emotions: List[str] = Field(default_factory=list)
    
    # Поля для координации LTM
    last_user_text: Optional[str] = None
    last_bot_response: Optional[str] = None
    last_bot_mode: Optional[str] = None
    last_bot_confidence: Optional[float] = None
    
    @field_validator('mode_confidence')
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        from config.settings import PYDANTIC_CONFIDENCE_MIN, PYDANTIC_CONFIDENCE_MAX
        if not PYDANTIC_CONFIDENCE_MIN <= v <= PYDANTIC_CONFIDENCE_MAX:
            raise ValueError(f'Mode confidence must be between {PYDANTIC_CONFIDENCE_MIN} and {PYDANTIC_CONFIDENCE_MAX}')
        return v
    
    @field_validator('current_mode')
    @classmethod
    def validate_mode(cls, v: str) -> str:
        valid_modes = ['talk', 'expert', 'creative', 'base']
        if v not in valid_modes:
            raise ValueError(f'Invalid mode: {v}. Must be one of: {valid_modes}')
        return v
    
    @field_validator('mode_history')
    @classmethod
    def validate_mode_history_size(cls, v: List[str]) -> List[str]:
        from config.settings import PYDANTIC_MODE_HISTORY_MAX_SIZE
        if len(v) > PYDANTIC_MODE_HISTORY_MAX_SIZE:
            # Обрезаем до максимального размера
            return v[-PYDANTIC_MODE_HISTORY_MAX_SIZE:]
        return v
    
    @field_validator('cache_metrics')
    @classmethod
    def validate_cache_metrics_size(cls, v: List[float]) -> List[float]:
        from config.settings import PYDANTIC_CACHE_METRICS_MAX_SIZE
        if len(v) > PYDANTIC_CACHE_METRICS_MAX_SIZE:
            # Обрезаем до максимального размера
            return v[-PYDANTIC_CACHE_METRICS_MAX_SIZE:]
        return v

class UserSessionActor(BaseActor, ModeDetectionMixin, PromptManagementMixin, RequestHandlingMixin, LTMCoordinationMixin):
    """
    Координатор сессий пользователей.
    Управляет жизненным циклом сессий и определяет необходимость системного промпта.
    """
    
    def __init__(self):
        super().__init__("user_session", "UserSession")
        self._sessions: Dict[str, UserSession] = {}
        self._event_version_manager = EventVersionManager()
        self._last_detection_details = {}
        self._pending_requests: Dict[str, Dict[str, Any]] = {}  # Для связывания контекстных запросов
        self._pending_limits: Dict[str, Dict[str, Any]] = {}  # Для CHECK_LIMIT запросов
        self._cleanup_task: Optional[asyncio.Task] = None  # Задача очистки зависших запросов
    
    async def initialize(self) -> None:
        """Инициализация актора"""
        # Запускаем периодическую очистку зависших запросов
        self._cleanup_task = asyncio.create_task(self._cleanup_pending_requests_loop())
        self.logger.info("UserSessionActor initialized")
        
    async def shutdown(self) -> None:
        """Освобождение ресурсов"""
        # Останавливаем задачу очистки
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
                
        session_count = len(self._sessions)
        self._sessions.clear()
        self.logger.info(f"UserSessionActor shutdown, cleared {session_count} sessions")
    
    @measure_latency
    async def handle_message(self, message: ActorMessage) -> Optional[ActorMessage]:
        """Обработка входящих сообщений"""
        
        # Обработка USER_MESSAGE
        if message.message_type == MESSAGE_TYPES['USER_MESSAGE']:
            generate_msg = await self._handle_user_message(message)
            # Отправляем в GenerationActor
            if generate_msg and self.get_actor_system():
                await self.get_actor_system().send_message("generation", generate_msg)
            
        # Обработка метрик кэша для адаптивной стратегии
        elif message.message_type == MESSAGE_TYPES['CACHE_HIT_METRIC']:
            await self._update_cache_metrics(message)
            
        # Обработка BOT_RESPONSE для сохранения в память
        elif message.message_type == MESSAGE_TYPES['BOT_RESPONSE']:
            # Сохраняем ответ бота для LTM координации
            user_id = message.payload.get('user_id')
            if user_id and user_id in self._sessions:
                session = self._sessions[user_id]
                session.last_bot_response = message.payload['text']
                session.last_bot_mode = session.current_mode
                session.last_bot_confidence = session.mode_confidence
                
                # Проверяем необходимость сохранения в LTM
                # К этому моменту у нас есть: user_text, emotions, bot_response
                if session.last_emotion_vector and session.last_user_text and session.last_bot_response:
                    if self._should_save_to_ltm(session.last_emotion_vector):
                        # Подготавливаем данные для оценки
                        ltm_payload = self._prepare_ltm_evaluation(
                            session=session,
                            user_text=session.last_user_text,
                            bot_response=session.last_bot_response,
                            emotions_data={
                                'emotions': session.last_emotion_vector,
                                'dominant_emotions': session.last_dominant_emotions
                            }
                        )
                        
                        # Отправляем на оценку в LTMActor
                        await self._request_ltm_evaluation(ltm_payload)
            
            # Сохраняем ответ бота в память
            if self.get_actor_system():
                store_msg = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['STORE_MEMORY'],
                    payload={
                        'user_id': message.payload['user_id'],
                        'message_type': 'bot',
                        'content': message.payload['text'],
                        'metadata': {
                            'generated_at': message.payload.get('generated_at', datetime.now().isoformat())
                        }
                    }
                )
                await self.get_actor_system().send_message("memory", store_msg)
        
        # Обработка CONTEXT_RESPONSE от MemoryActor
        elif message.message_type == MESSAGE_TYPES['CONTEXT_RESPONSE']:
            request_id = message.payload.get('request_id')
            if not request_id or request_id not in self._pending_requests:
                self.logger.warning(f"Received CONTEXT_RESPONSE with unknown request_id: {request_id}")
                return None
            
            # Извлекаем сохраненный контекст
            pending = self._pending_requests.pop(request_id)
            
            # Создаем сообщение для GenerationActor с историческим контекстом
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
                    'historical_context': message.payload.get('messages', [])  # Контекст из памяти
                }
            )
            
            self.logger.info(
                f"Created GENERATE_RESPONSE for user {pending['user_id']} "
                f"with {len(message.payload.get('messages', []))} historical messages"
            )
            
            # Отправляем в GenerationActor
            if self.get_actor_system():
                await self.get_actor_system().send_message("generation", generate_msg)
            
        # Обработка LIMIT_RESPONSE от AuthActor
        elif message.message_type == MESSAGE_TYPES['LIMIT_RESPONSE']:
            request_id = message.payload.get('request_id')
            if not request_id or request_id not in self._pending_limits:
                self.logger.warning(f"Received LIMIT_RESPONSE with unknown request_id: {request_id}")
                return None
            
            # Извлечь сохраненный контекст
            pending = self._pending_limits.pop(request_id)
            
            # Проверить лимиты
            unlimited = message.payload.get('unlimited', False)
            messages_today = message.payload.get('messages_today', 0)
            limit = message.payload.get('limit', DAILY_MESSAGE_LIMIT)
            
            # Если демо-пользователь превысил лимит
            if not unlimited and messages_today >= limit:
                self.logger.warning(
                    f"User {pending['user_id']} exceeded daily limit: "
                    f"{messages_today}/{limit} messages"
                )
                
                # Создаем событие превышения лимита
                from actors.events import LimitExceededEvent
                limit_event = LimitExceededEvent.create(
                    user_id=pending['user_id'],
                    messages_today=messages_today,
                    daily_limit=limit
                )
                await self._event_version_manager.append_event(limit_event, self.get_actor_system())
                self.logger.info(f"Created LimitExceededEvent for user {pending['user_id']}")
                
                # Отправить уведомление пользователю
                limit_exceeded_msg = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['LIMIT_EXCEEDED'],
                    payload={
                        'user_id': pending['user_id'],
                        'chat_id': pending['chat_id'],
                        'messages_today': messages_today,
                        'limit': limit
                    }
                )
                
                if self.get_actor_system():
                    await self.get_actor_system().send_message("telegram", limit_exceeded_msg)
                    self.logger.info(f"Sent LIMIT_EXCEEDED to telegram for user {pending['user_id']}")
                                
                return None
            
            # Если лимит не превышен - продолжить обработку
            self.logger.info(f"User {pending['user_id']} within limits, processing message")
            await self._continue_message_processing(pending)
            
        # Обработка EMOTION_RESULT от PerceptionActor
        elif message.message_type == MESSAGE_TYPES['EMOTION_RESULT']:
            user_id = message.payload.get('user_id')
            if not user_id:
                self.logger.warning("Received EMOTION_RESULT without user_id")
                return None
            
            # Извлекаем эмоции для логирования
            dominant_emotions = message.payload.get('dominant_emotions', [])
            emotion_scores = message.payload.get('emotions', {})
            
            # Находим топ-3 эмоции с их вероятностями
            if emotion_scores:
                top_emotions = sorted(emotion_scores.items(), key=lambda x: x[1], reverse=True)[:3]
                emotions_str = ", ".join([f"{emotion}: {score:.2f}" for emotion, score in top_emotions])
                
                emoji = EMOTION_EMOJI_MAP.get(dominant_emotions[0], '🎭') if dominant_emotions else '🎭'
                self.logger.info(
                    # f"{emoji} Emotions for user {user_id}: [{emotions_str}] | Dominant: {dominant_emotions}"
                    f"{emoji} [{emotions_str}] → {dominant_emotions}"
                )
            else:
                self.logger.info(f"Received EMOTION_RESULT for user {user_id} (no emotions detected)")
            
            # Получаем сессию
            if user_id in self._sessions:
                session = self._sessions[user_id]
                
                # Сохраняем эмоции в сессии
                session.last_emotion_vector = message.payload.get('emotions', {})
                session.last_dominant_emotions = message.payload.get('dominant_emotions', [])
                
                # Создаем событие
                try:
                    event = EmotionDetectedEvent.create(
                        user_id=user_id,
                        dominant_emotions=session.last_dominant_emotions,
                        emotion_scores=session.last_emotion_vector,
                        text_preview=message.payload.get('text', '')
                    )
                    
                    await self._event_version_manager.append_event(event, self.get_actor_system())
                    self.logger.info(f"Saved EmotionDetectedEvent for user {user_id}")
                    
                except Exception as e:
                    self.logger.error(f"Failed to save EmotionDetectedEvent: {str(e)}")
            else:
                self.logger.warning(f"No session found for user {user_id}")
        
        return None
    
    async def _handle_user_message(self, message: ActorMessage) -> ActorMessage:
        """Обработка сообщения от пользователя"""
        user_id = message.payload['user_id']
        username = message.payload.get('username')
        text = message.payload['text']
        chat_id = message.payload['chat_id']
        
        # Получаем или создаем сессию
        session = await self._get_or_create_session(user_id, username)
        
        # Сохраняем полный текст для LTM координации
        session.last_user_text = text
        
        # Сохраняем сообщение пользователя в память
        if self.get_actor_system():
            store_msg = ActorMessage.create(
                sender_id=self.actor_id,
                message_type=MESSAGE_TYPES['STORE_MEMORY'],
                payload={
                    'user_id': user_id,
                    'message_type': 'user',
                    'content': text,
                    'metadata': {
                        'username': username,
                        'timestamp': datetime.now().isoformat()
                    }
                }
            )
            await self.get_actor_system().send_message("memory", store_msg)
        
        # Отправляем запрос на проверку лимитов (fire-and-forget в этом этапе)
        limit_request_id = str(uuid.uuid4())
        self._pending_limits[limit_request_id] = {
            'user_id': user_id,
            'timestamp': datetime.now(),
            'chat_id': chat_id,
            'text': text,
            'username': username,
            'session': session,
            'message': message
        }
        
        check_limit_msg = ActorMessage.create(
            sender_id=self.actor_id,
            message_type=MESSAGE_TYPES['CHECK_LIMIT'],
            payload={
                'user_id': user_id,
                'request_id': limit_request_id  # Для связывания с ответом
            }
        )
        
        await self.get_actor_system().send_message("auth", check_limit_msg)
        self.logger.info(f"Sent CHECK_LIMIT for user {user_id}, request_id: {limit_request_id}")
        
        # Ждем LIMIT_RESPONSE перед продолжением
        return None
        analyze_msg = ActorMessage.create(
            sender_id=self.actor_id,
            message_type=MESSAGE_TYPES['ANALYZE_EMOTION'],
            payload={
                'user_id': user_id,
                'text': text
            },
            reply_to=self.actor_id  # Добавлена эта строка!
        )
        await self.get_actor_system().send_message("perception", analyze_msg)
        # self.logger.info(f"Sent ANALYZE_EMOTION for user {user_id}")
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
        from config.settings import MODE_HISTORY_SIZE
        session.mode_history.append(new_mode)
        if len(session.mode_history) > MODE_HISTORY_SIZE:
            session.mode_history.pop(0)
        
        # Логирование для отладки
        self.logger.info(
            f"Mode detection: {new_mode} "
            # f"Mode detection for user {user_id}: {new_mode} "
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
        # self.logger.info(f"Requested context for user {user_id}, request_id: {request_id}")
        self.logger.info(f"Requested context for user {user_id}")
        
        # НЕ возвращаем сообщение - ждем CONTEXT_RESPONSE
        return None
    
    async def _get_or_create_session(self, user_id: str, username: Optional[str]) -> UserSession:
        """Получить существующую или создать новую сессию"""
        if user_id not in self._sessions:
            session = UserSession(user_id=user_id, username=username)
            self._sessions[user_id] = session
            
            # Событие о создании сессии
            event = BaseEvent.create(
                stream_id=f"user_{user_id}",
                event_type="SessionCreatedEvent",
                data={
                    "user_id": user_id,
                    "username": username,
                    "created_at": session.created_at.isoformat()
                }
            )
            
            # Сохраняем событие
            await self._append_event(event)
            
            self.logger.info(f"Created new session for user {user_id}")
        
        return self._sessions[user_id]
    
    async def _append_event(self, event: BaseEvent) -> None:
        """Добавить событие через менеджер версий"""
        await self._event_version_manager.append_event(event, self.get_actor_system())