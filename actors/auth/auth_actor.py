"""
AuthActor - актор для управления авторизацией и контроля доступа.
Обрабатывает проверку лимитов, авторизацию паролем и админские команды.

АРХИТЕКТУРНОЕ РЕШЕНИЕ: AuthActor использует прямой доступ к БД

Это инфраструктурный сервис, не влияющий на поведение Химеры.
Прямой доступ оправдан производительностью и изоляцией от core.
"""

from typing import Optional
import asyncio
from datetime import datetime, timezone, timedelta
from actors.auth.admin_handler import AuthAdminHandler
from actors.auth.helpers import AuthHelpers
from actors.base_actor import BaseActor
from actors.messages import ActorMessage, MESSAGE_TYPES
from database.connection import db_connection
from utils.monitoring import measure_latency
from utils.event_utils import EventVersionManager
from config.settings import (
    AUTH_SCHEMA_CHECK_TIMEOUT,
    AUTH_CLEANUP_INTERVAL,
    AUTH_METRICS_LOG_INTERVAL,
    DAILY_MESSAGE_LIMIT
)
import hashlib
from database.redis_connection import redis_connection
from utils.circuit_breaker import CircuitBreaker


class AuthActor(BaseActor, AuthAdminHandler, AuthHelpers):
    """
    Актор для управления авторизацией и контроля доступа.
    
    Основные функции:
    - Проверка дневных лимитов для демо-пользователей
    - Авторизация через временные пароли
    - Управление подписками
    - Администрирование паролей
    - Anti-bruteforce защита
    """
    
    def __init__(self):
        """Инициализация с фиксированным actor_id и именем"""
        super().__init__("auth", "Auth")
        self._pool = None
        self._degraded_mode = False
        self._event_version_manager = EventVersionManager()
        self._redis_connection = None  # Redis подключение
        
        # Метрики для отслеживания работы
        self._metrics = {
            'initialized': False,
            'degraded_mode_entries': 0,
            'check_limit_count': 0,
            'auth_request_count': 0,
            'auth_success_count': 0,
            'auth_failed_count': 0,
            'blocked_users_count': 0,
            'admin_commands_count': 0,
            'db_errors': 0
        }
        
        # Задачи для фоновых операций
        self._cleanup_task = None
        self._metrics_task = None
        self._auth_circuit_breakers = {}  # user_id -> CircuitBreaker
        self._daily_reset_task = None
        
    async def initialize(self) -> None:
        """Инициализация ресурсов актора"""
        try:
            # Проверяем подключение к БД
            if not db_connection._is_connected:
                await db_connection.connect()
            
            # Получаем пул подключений
            self._pool = db_connection.get_pool()
            
            # Проверяем схему БД
            await self._verify_schema()
            
            # Запускаем фоновые задачи
            if AUTH_CLEANUP_INTERVAL > 0:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                
            if AUTH_METRICS_LOG_INTERVAL > 0:
                self._metrics_task = asyncio.create_task(self._metrics_loop())
                
            # Запускаем задачу ежедневного сброса
            from config.settings import AUTH_DAILY_RESET_ENABLED
            if AUTH_DAILY_RESET_ENABLED:
                self._daily_reset_task = asyncio.create_task(self._daily_reset_loop())
                self.logger.info("Started daily reset task")
            
            # Подключаемся к Redis
            await redis_connection.connect()
            self._redis_connection = redis_connection
            
            self._metrics['initialized'] = True
            self.logger.info("AuthActor initialized successfully with Redis support")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize AuthActor: {str(e)}")
            self._degraded_mode = True
            self._metrics['degraded_mode_entries'] += 1
            self._increment_metric('db_errors')
            self.logger.warning("AuthActor entering degraded mode - will work without persistence")
    
    async def shutdown(self) -> None:
        """Освобождение ресурсов актора"""
        # Останавливаем фоновые задачи
        for task in [self._cleanup_task, self._metrics_task, self._daily_reset_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Отключаемся от Redis
        if self._redis_connection:
            await redis_connection.disconnect()
        
        # Логируем финальные метрики
        self.logger.info(
            f"AuthActor shutdown. Metrics: "
            f"Check limits: {self._metrics['check_limit_count']}, "
            f"Auth requests: {self._metrics['auth_request_count']}, "
            f"Auth success: {self._metrics['auth_success_count']}, "
            f"Auth failed: {self._metrics['auth_failed_count']}, "
            f"Blocked users: {self._metrics['blocked_users_count']}, "
            f"Admin commands: {self._metrics['admin_commands_count']}, "
            f"DB errors: {self._metrics['db_errors']}"
        )
    
    @measure_latency
    async def handle_message(self, message: ActorMessage) -> Optional[ActorMessage]:
        """Обработка входящих сообщений"""
        
        # Проверка лимитов пользователя
        if message.message_type == MESSAGE_TYPES['CHECK_LIMIT']:
            self._metrics['check_limit_count'] += 1
            await self._handle_check_limit(message)
            
        # Запрос авторизации
        elif message.message_type == MESSAGE_TYPES['AUTH_REQUEST']:
            self._metrics['auth_request_count'] += 1
            
            # Извлекаем данные
            user_id = message.payload.get('user_id')
            password = message.payload.get('password')
            chat_id = message.payload.get('chat_id')
            
            if not user_id or not password:
                self.logger.warning("AUTH_REQUEST received without user_id or password")
                return None
            
            self.logger.debug(f"Processing AUTH_REQUEST for user {user_id}")
            
            # Circuit Breaker проверка
            from config.settings import AUTH_CIRCUIT_BREAKER_ENABLED, AUTH_CIRCUIT_BREAKER_TIMEOUT
            if AUTH_CIRCUIT_BREAKER_ENABLED:
                if user_id not in self._auth_circuit_breakers:
                    from config.settings import AUTH_CIRCUIT_BREAKER_THRESHOLD
                    self._auth_circuit_breakers[user_id] = CircuitBreaker(
                        name=f"auth_{user_id}",
                        failure_threshold=AUTH_CIRCUIT_BREAKER_THRESHOLD,
                        recovery_timeout=AUTH_CIRCUIT_BREAKER_TIMEOUT,
                        expected_exception=Exception  # Любые ошибки авторизации
                    )
                
                try:
                    # Проверяем состояние Circuit Breaker
                    cb = self._auth_circuit_breakers[user_id]
                    if cb.state.value == "open":
                        # Circuit Breaker открыт - сразу отклоняем
                        self.logger.warning(f"Circuit breaker OPEN for user {user_id}, rejecting auth")
                        
                        # Создаем событие брутфорса
                        from actors.events import BruteforceDetectedEvent
                        bruteforce_event = BruteforceDetectedEvent.create(
                            user_id=user_id,
                            attempts_count=cb._failure_count,
                            action_taken="circuit_breaker_rejection"
                        )
                        await self._event_version_manager.append_event(bruteforce_event, self.get_actor_system())
                        
                        response = ActorMessage.create(
                            sender_id=self.actor_id,
                            message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                            payload={
                                'user_id': user_id,
                                'chat_id': chat_id,
                                'success': False,
                                'error': 'blocked',
                                'blocked_until': (datetime.now(timezone.utc) + timedelta(seconds=AUTH_CIRCUIT_BREAKER_TIMEOUT)).isoformat()
                            }
                        )
                        if self.get_actor_system() and message.sender_id:
                            await self.get_actor_system().send_message(message.sender_id, response)
                        return None
                        
                except Exception as e:
                    self.logger.error(f"Circuit breaker check error: {str(e)}")
            
            # ПЕРВЫМ делом проверяем блокировку
            is_blocked, blocked_until = await self._check_user_blocked(user_id)
            if is_blocked:
                response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                    payload={
                        'user_id': user_id,
                        'chat_id': chat_id,
                        'success': False,
                        'error': 'blocked',
                        'blocked_until': blocked_until.isoformat() if blocked_until else None
                    }
                )
                if self.get_actor_system() and message.sender_id:
                    await self.get_actor_system().send_message(message.sender_id, response)
                return None
            
            # В degraded mode не обрабатываем
            if self._degraded_mode or not self._pool:
                response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                    payload={
                        'user_id': user_id,
                        'chat_id': chat_id,
                        'success': False,
                        'error': 'temporary_error'
                    }
                )
                if self.get_actor_system() and message.sender_id:
                    await self.get_actor_system().send_message(message.sender_id, response)
                return None
            
            try:
                from config.settings import AUTH_MAX_ATTEMPTS
                
                # Хешируем пароль
                password_hash = hashlib.sha256(password.encode()).hexdigest()
                
                # 1. Ищем пароль в БД
                password_query = """
                    SELECT password_hash, duration_days, description, used_by, expires_at
                    FROM passwords
                    WHERE password = $1 AND is_active = TRUE
                """
                
                password_row = await self._pool.fetchrow(password_query, password)
                
                if not password_row:
                    # Пароль не найден
                    self.logger.debug(f"Password not found for user {user_id}")
                    # Логируем неудачную попытку
                    await self._pool.execute(
                        """
                        INSERT INTO auth_attempts (user_id, password_attempt, success, error_reason, timestamp)
                        VALUES ($1, $2, FALSE, 'invalid', CURRENT_TIMESTAMP)
                        """,
                        user_id, password
                    )
                    
                    # Проверяем количество попыток
                    failed_count = await self._increment_failed_attempts(user_id)
                    if failed_count >= AUTH_MAX_ATTEMPTS:
                        await self._block_user(user_id, failed_count)
                    
                    # Увеличиваем метрику
                    self._metrics['auth_failed_count'] += 1
                    
                    # Отправляем ответ об ошибке
                    response = ActorMessage.create(
                        sender_id=self.actor_id,
                        message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                        payload={
                            'user_id': user_id,
                            'chat_id': chat_id,
                            'success': False,
                            'error': 'invalid_password'
                        }
                    )
                    if self.get_actor_system() and message.sender_id:
                        await self.get_actor_system().send_message(message.sender_id, response)
                    return None
                
                # Проверяем хеш
                if password_row['password_hash'] != password_hash:
                    self.logger.debug(f"Invalid password hash for user {user_id}")
                    # Логируем неудачную попытку
                    await self._pool.execute(
                        """
                        INSERT INTO auth_attempts (user_id, password_attempt, success, error_reason, timestamp)
                        VALUES ($1, $2, FALSE, 'invalid', CURRENT_TIMESTAMP)
                        """,
                        user_id, password
                    )
                    
                    # Проверяем количество попыток
                    failed_count = await self._increment_failed_attempts(user_id)
                    if failed_count >= AUTH_MAX_ATTEMPTS:
                        await self._block_user(user_id, failed_count)
                    
                    # Увеличиваем метрику
                    self._metrics['auth_failed_count'] += 1
                    
                    # Отправляем ответ об ошибке
                    response = ActorMessage.create(
                        sender_id=self.actor_id,
                        message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                        payload={
                            'user_id': user_id,
                            'chat_id': chat_id,
                            'success': False,
                            'error': 'invalid_password'
                        }
                    )
                    if self.get_actor_system() and message.sender_id:
                        await self.get_actor_system().send_message(message.sender_id, response)
                    return None
                
                # 2. Проверяем, нужно ли обновлять подписку
                if password_row['used_by'] == user_id:
                    # Повторная авторизация тем же паролем - ничего не обновляем
                    self.logger.debug(f"Re-authentication with same password for user {user_id}")
                    bind_result = True  # Пароль уже привязан к этому пользователю
                    
                    # Получаем существующий expires_at из authorized_users
                    auth_query = "SELECT expires_at FROM authorized_users WHERE user_id = $1"
                    auth_row = await self._pool.fetchrow(auth_query, user_id)
                    if auth_row:
                        expires_at = auth_row['expires_at'].replace(tzinfo=timezone.utc)
                    else:
                        # Если записи нет, вычисляем новый expires_at
                        expires_at = datetime.now(timezone.utc) + timedelta(days=password_row['duration_days'])
                    
                else:
                    # Первое использование или попытка использовать чужой пароль
                    expires_at = datetime.now(timezone.utc) + timedelta(days=password_row['duration_days'])
                    
                    # 3. Привязываем пароль к пользователю
                    bind_result = await self._pool.fetchval(
                        "SELECT bind_password_to_user($1, $2, $3) as success",
                        password,
                        user_id,
                        expires_at
                    )
                
                if not bind_result:
                    # Пароль уже использован другим пользователем
                    self.logger.debug("Password already used by another user")
                    # Логируем неудачную попытку
                    await self._pool.execute(
                        """
                        INSERT INTO auth_attempts (user_id, password_attempt, success, error_reason, timestamp)
                        VALUES ($1, $2, FALSE, 'already_used', CURRENT_TIMESTAMP)
                        """,
                        user_id, password
                    )
                    
                    # НЕ увеличиваем счетчик попыток для already_used
                    # Увеличиваем метрику
                    self._metrics['auth_failed_count'] += 1
                    
                    # Отправляем ответ об ошибке
                    response = ActorMessage.create(
                        sender_id=self.actor_id,
                        message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                        payload={
                            'user_id': user_id,
                            'chat_id': chat_id,
                            'success': False,
                            'error': 'already_used'
                        }
                    )
                    if self.get_actor_system() and message.sender_id:
                        await self.get_actor_system().send_message(message.sender_id, response)
                    return None
                
                self.logger.debug(f"Password bound to user {user_id}")
                
                # 4. Создаем/обновляем подписку только для новых паролей
                if password_row['used_by'] != user_id:
                    # Новый пароль - создаем или обновляем подписку
                    await self._pool.execute(
                        """
                        INSERT INTO authorized_users (user_id, password_used, expires_at, authorized_at, description)
                        VALUES ($1, $2, $3, CURRENT_TIMESTAMP, $4)
                        ON CONFLICT (user_id) DO UPDATE
                        SET password_used = $2, 
                            expires_at = $3,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        user_id,
                        password,
                        expires_at,
                        password_row['description']
                    )
                else:
                    # Повторная авторизация - создаем запись если её нет (после logout)
                    await self._pool.execute(
                        """
                        INSERT INTO authorized_users (user_id, password_used, expires_at, authorized_at, description)
                        VALUES ($1, $2, $3, CURRENT_TIMESTAMP, $4)
                        ON CONFLICT (user_id) DO UPDATE
                        SET updated_at = CURRENT_TIMESTAMP
                        """,
                        user_id,
                        password,
                        expires_at,
                        password_row['description']
                    )
                
                # 5. Логируем успешную попытку
                await self._pool.execute(
                    """
                    INSERT INTO auth_attempts (user_id, password_attempt, success, timestamp)
                    VALUES ($1, $2, TRUE, CURRENT_TIMESTAMP)
                    """,
                    user_id, password
                )
                self.logger.debug(f"Auth attempt logged for user {user_id}")
                
                # 6. Создаем события
                from actors.events.auth_events import AuthSuccessEvent, PasswordUsedEvent
                
                # Событие успешной авторизации
                success_event = AuthSuccessEvent.create(
                    user_id=user_id,
                    password=password,
                    expires_at=expires_at,
                    description=password_row['description']
                )
                await self._event_version_manager.append_event(success_event, self.get_actor_system())
                
                # Событие использования пароля (только при первом использовании)
                if password_row['used_by'] is None:
                    used_event = PasswordUsedEvent.create(
                        password=password,
                        used_by=user_id,
                        expires_at=expires_at
                    )
                    await self._event_version_manager.append_event(used_event, self.get_actor_system())
                
                # 7. Отправляем успешный ответ
                response_payload = {
                    'user_id': user_id,
                    'chat_id': chat_id,
                    'success': True,
                    'expires_at': expires_at.isoformat(),
                    'days_remaining': (expires_at - datetime.now(timezone.utc)).days,
                    'description': password_row['description']
                }
                
                response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                    payload=response_payload
                )
                
                if self.get_actor_system() and message.sender_id:
                    await self.get_actor_system().send_message(message.sender_id, response)
                
                # Обновляем метрики
                self._metrics['auth_success_count'] += 1
                
                # Сбрасываем Circuit Breaker при успешной авторизации
                if user_id in self._auth_circuit_breakers:
                    self._auth_circuit_breakers[user_id].reset()
                    self.logger.debug(f"Reset circuit breaker for user {user_id}")
                
                self.logger.info(
                    f"User {user_id} successfully authorized until {expires_at.isoformat()}"
                )
                
            except Exception as e:
                self.logger.error(f"Error processing AUTH_REQUEST for user {user_id}: {str(e)}", exc_info=True)
                self._increment_metric('db_errors')
                
                # При любой ошибке - отправляем неуспешный ответ
                response = ActorMessage.create(
                    sender_id=self.actor_id,
                    message_type=MESSAGE_TYPES['AUTH_RESPONSE'],
                    payload={
                        'user_id': user_id,
                        'chat_id': chat_id,
                        'success': False,
                        'error': 'temporary_error'
                    }
                )
                if self.get_actor_system() and message.sender_id:
                    await self.get_actor_system().send_message(message.sender_id, response)
            
        # Запрос на выход
        elif message.message_type == MESSAGE_TYPES['LOGOUT_REQUEST']:
            user_id = message.payload.get('user_id')
            chat_id = message.payload.get('chat_id')
            
            if not user_id:
                self.logger.warning("LOGOUT_REQUEST without user_id")
                return None
                
            # Проверяем, авторизован ли пользователь
            try:
                if self._pool:
                    # Проверяем наличие активной подписки
                    check_query = """
                        SELECT EXISTS(
                            SELECT 1 FROM authorized_users 
                            WHERE user_id = $1 AND expires_at > CURRENT_TIMESTAMP
                        )
                    """
                    is_authorized = await self._pool.fetchval(check_query, user_id)
                    
                    if is_authorized:
                        # Удаляем запись
                        delete_query = "DELETE FROM authorized_users WHERE user_id = $1"
                        await self._pool.execute(delete_query, user_id)
                        
                        # Создаем событие
                        from actors.events import BaseEvent
                        logout_event = BaseEvent.create(
                            stream_id=f"auth_{user_id}",
                            event_type="LogoutEvent",
                            data={
                                "user_id": user_id,
                                "timestamp": datetime.now().isoformat()
                            }
                        )
                        await self._event_version_manager.append_event(logout_event, self.get_actor_system())
                        
                        success = True
                        self.logger.info(f"User {user_id} logged out successfully")
                    else:
                        success = False
                        self.logger.debug(f"User {user_id} was not authorized")
                else:
                    # В degraded mode
                    success = False
                    
            except Exception as e:
                self.logger.error(f"Error processing LOGOUT_REQUEST: {str(e)}")
                self._increment_metric('db_errors')
                success = False
            
            # Отправляем ответ
            response = ActorMessage.create(
                sender_id=self.actor_id,
                message_type=MESSAGE_TYPES['LOGOUT_RESPONSE'],
                payload={
                    'user_id': user_id,
                    'chat_id': chat_id,
                    'success': success
                }
            )
            
            if self.get_actor_system() and message.sender_id:
                await self.get_actor_system().send_message(message.sender_id, response)
            
        # Админская команда
        elif message.message_type == MESSAGE_TYPES['ADMIN_COMMAND']:
            self._metrics['admin_commands_count'] += 1
            await self._handle_admin_command(message)
            
        else:
            self.logger.warning(
                f"Unknown message type received: {message.message_type}"
            )
        
        return None  # Fire-and-forget паттерн
    
    async def _handle_check_limit(self, message: ActorMessage) -> None:
        """Обработка запроса на проверку лимитов пользователя"""
        # Извлекаем user_id
        user_id = message.payload.get('user_id')
        if not user_id:
            self.logger.warning("CHECK_LIMIT received without user_id")
            return
        
        self.logger.debug(f"Checking limit for user {user_id}")
        
        # Извлекаем дополнительные данные из запроса
        chat_id = message.payload.get('chat_id')
        is_status_check = message.payload.get('is_status_check', False)
        
        # Значения по умолчанию для демо-пользователя
        response_payload = {
            'user_id': user_id,
            'chat_id': chat_id,
            'is_status_check': is_status_check,
            'unlimited': False,
            'messages_today': 0,  # Временно всегда 0
            'limit': DAILY_MESSAGE_LIMIT,
            'expires_at': None,
        }
        
        # Проверяем в БД только если не в degraded mode
        if not self._degraded_mode and self._pool:
            try:
                # Запрос к БД
                query = """
                    SELECT expires_at, password_used 
                    FROM authorized_users 
                    WHERE user_id = $1
                """
                
                row = await self._pool.fetchrow(query, user_id)
                
                if row:
                    expires_at = row['expires_at']
                    # Проверяем, не истекла ли подписка
                    if expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
                        # Активная подписка
                        response_payload = {
                            'user_id': user_id,
                            'chat_id': chat_id,
                            'is_status_check': is_status_check,
                            'unlimited': True,
                            'messages_today': 0,  # Временно всегда 0
                            'limit': None,
                            'expires_at': expires_at.isoformat(),
                        }
                        self.logger.info(
                            f"User {user_id} has active subscription until {expires_at.isoformat()}"
                        )
                    else:
                        # Подписка истекла
                        self.logger.debug(f"User {user_id} subscription expired at {expires_at.isoformat()}")
                else:
                    # Пользователь не найден
                    self.logger.debug(f"User {user_id} using demo access")
                    
            except Exception as e:
                self.logger.error(f"Database error checking limit for user {user_id}: {str(e)}", exc_info=True)
                self._increment_metric('db_errors')
                # При ошибке БД используем демо-лимит (fail-open)
        
        # Для демо-пользователей проверяем счетчики Redis
        if not response_payload['unlimited']:
            messages_today = await self._get_daily_message_count(user_id)
            if messages_today is None:
                # Redis недоступен - разрешаем в degraded mode
                self.logger.warning(f"Redis unavailable, allowing user {user_id} in degraded mode")
                messages_today = 0
            
            # Обновляем payload
            response_payload['messages_today'] = messages_today
            
            # Если это не просто проверка статуса - увеличиваем счетчик
            if not is_status_check and messages_today < response_payload['limit']:
                new_count = await self._increment_daily_message_count(user_id)
                if new_count is not None:
                    response_payload['messages_today'] = new_count
                else:
                    # Redis недоступен - все равно увеличиваем локальное значение
                    response_payload['messages_today'] = messages_today + 1
            
            self.logger.debug(f"Demo user {user_id}: {response_payload['messages_today']}/{response_payload['limit']} messages today")
        
        # Добавляем request_id в payload
        response_payload['request_id'] = message.payload.get('request_id')
        
        # Отправляем ответ
        if self.get_actor_system() and message.sender_id:
            response = ActorMessage.create(
                sender_id=self.actor_id,
                message_type=MESSAGE_TYPES['LIMIT_RESPONSE'],
                payload=response_payload
            )
            await self.get_actor_system().send_message(message.sender_id, response)
            self.logger.debug(f"Sent LIMIT_RESPONSE to {message.sender_id} for user {user_id}")
    
    async def _verify_schema(self) -> None:
        """Проверка существования таблиц БД"""
        try:
            if self._pool is None:
                raise RuntimeError("Database pool not initialized")
            
            # Проверяем существование всех таблиц авторизации
            required_tables = ['passwords', 'authorized_users', 'auth_attempts', 'blocked_users']
            
            query = """
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = ANY($1)
            """
            
            rows = await self._pool.fetch(
                query, 
                required_tables,
                timeout=AUTH_SCHEMA_CHECK_TIMEOUT
            )
            
            existing_tables = {row['table_name'] for row in rows}
            missing_tables = set(required_tables) - existing_tables
            
            if missing_tables:
                raise RuntimeError(
                    f"Required auth tables missing: {', '.join(missing_tables)}. "
                    f"Please run migration 003_create_auth_tables.sql"
                )
            
            self.logger.debug("Auth schema verification completed successfully")
            
        except Exception as e:
            self.logger.error(f"Schema verification failed: {str(e)}")
            raise
    
    async def _cleanup_loop(self) -> None:
        """Периодическая очистка старых данных"""
        while self.is_running:
            try:
                await asyncio.sleep(AUTH_CLEANUP_INTERVAL)
                
                # Очистка старых попыток авторизации
                # TODO: реализация в подэтапе 5.1.3
                
                self.logger.debug("Auth cleanup completed")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in cleanup loop: {str(e)}")
    
    async def _metrics_loop(self) -> None:
        """Периодическое логирование метрик"""
        while self.is_running:
            try:
                await asyncio.sleep(AUTH_METRICS_LOG_INTERVAL)
                
                if self._metrics['check_limit_count'] > 0 or self._metrics['auth_request_count'] > 0:
                    self.logger.info(
                        f"AuthActor metrics - "
                        f"Limits checked: {self._metrics['check_limit_count']}, "
                        f"Auth requests: {self._metrics['auth_request_count']}, "
                        f"Success rate: {self._calculate_success_rate():.1%}, "
                        f"Degraded mode: {self._degraded_mode}"
                    )
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in metrics loop: {str(e)}")
    
    async def _daily_reset_loop(self) -> None:
        """Периодический сброс дневных счетчиков"""
        from config.settings import AUTH_DAILY_RESET_HOUR
        
        while self.is_running:
            try:
                # Вычисляем время до следующего сброса
                now = datetime.now()
                next_reset = now.replace(hour=AUTH_DAILY_RESET_HOUR, minute=0, second=0, microsecond=0)
                
                # Если время уже прошло сегодня, планируем на завтра
                if next_reset <= now:
                    next_reset += timedelta(days=1)
                
                # Вычисляем задержку в секундах
                delay = (next_reset - now).total_seconds()
                
                self.logger.info(
                    f"Next daily reset scheduled at {next_reset.isoformat()}, "
                    f"waiting {delay:.0f} seconds"
                )
                
                # Ждем до времени сброса
                await asyncio.sleep(delay)
                
                # Выполняем сброс
                await self._reset_daily_counters()
                
                # Небольшая задержка чтобы не зациклиться
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in daily reset loop: {str(e)}")
                # При ошибке ждем час и пробуем снова
                await asyncio.sleep(3600)
    
    # Вспомогательные методы-заглушки для будущей реализации
    
    def _increment_metric(self, metric_name: str, value: int = 1) -> None:
        """Инкремент метрики"""
        if metric_name in self._metrics:
            self._metrics[metric_name] += value
    
    def _increment_metric(self, metric_name: str, value: int = 1) -> None:
        """Инкремент метрики"""
        if metric_name in self._metrics:
            self._metrics[metric_name] += value
    
    def _calculate_success_rate(self) -> float:
        """Вычисление процента успешных авторизаций"""
        total = self._metrics['auth_request_count']
        if total == 0:
            return 0.0
        return self._metrics['auth_success_count'] / total
        
    async def _handle_admin_command(self, message: ActorMessage) -> None:
        """Обработка админских команд"""
        command = message.payload.get('command', '')
        args = message.payload.get('args', [])
        user_id = message.payload.get('user_id')
        chat_id = message.payload.get('chat_id')
        
        self.logger.info(f"Processing admin command '{command}' from user {user_id}")
        
        # В degraded mode не обрабатываем админские команды
        if self._degraded_mode or not self._pool:
            response_text = "⚠️ Система авторизации временно недоступна"
        else:
            # Базовый роутинг команд (будет расширен в следующих частях)
            if command == 'admin_add_password':
                response_text = await self._admin_add_password(args, user_id)
            elif command == 'admin_list_passwords':
                response_text = await self._admin_list_passwords(args)
            elif command == 'admin_deactivate_password':
                response_text = await self._admin_deactivate_password(args, user_id)
            elif command == 'admin_stats':
                response_text = await self._admin_stats()
            elif command == 'admin_auth_log':
                response_text = await self._admin_auth_log(args)
            elif command == 'admin_blocked_users':
                response_text = await self._admin_blocked_users()
            elif command == 'admin_unblock_user':
                response_text = await self._admin_unblock_user(args)
            else:
                from config.messages import ADMIN_MESSAGES
                response_text = ADMIN_MESSAGES["unknown_command"].format(command=command)
        
        # Отправляем ответ обратно в TelegramActor
        response = ActorMessage.create(
            sender_id=self.actor_id,
            message_type=MESSAGE_TYPES['ADMIN_RESPONSE'],
            payload={
                'chat_id': chat_id,
                'text': response_text
            }
        )
        
        if self.get_actor_system() and message.sender_id:
            await self.get_actor_system().send_message(message.sender_id, response)