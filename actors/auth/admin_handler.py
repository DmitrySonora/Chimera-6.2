"""
Обработчики админских команд для AuthActor
"""
from datetime import datetime, timezone, timedelta
import hashlib
from typing import Optional
from config.messages import ADMIN_MESSAGES
from config.settings import PASSWORD_DURATIONS


class AuthAdminHandler:
    """Миксин с админскими командами для AuthActor"""
    
    # Эти атрибуты доступны из AuthActor
    _pool: Optional[object]
    _event_version_manager: object
    logger: object
    
    async def _admin_add_password(self, args: list, admin_id: str) -> str:
        """Создание нового пароля"""
        
        # Базовая проверка
        if len(args) < 1:
            return ADMIN_MESSAGES["password_usage"]
        
        password = args[0]
        
        # Если есть второй аргумент - проверяем его формат
        if len(args) >= 2:
            try:
                days = int(args[1])
            except ValueError:
                return ADMIN_MESSAGES["password_invalid_days_format"]
                
            if days not in PASSWORD_DURATIONS:
                return ADMIN_MESSAGES["password_invalid_days"].format(
                    durations=", ".join(map(str, PASSWORD_DURATIONS))
                )
        
        # Теперь проверяем полное количество аргументов
        if len(args) < 3:
            return ADMIN_MESSAGES["password_usage"]
        
        description = " ".join(args[2:])
        
        try:
            # Проверяем существование пароля
            existing = await self._pool.fetchrow(
                "SELECT 1 FROM passwords WHERE password = $1",
                password
            )
            
            if existing:
                return ADMIN_MESSAGES["password_already_exists"].format(password=password)
            
            # Хешируем пароль
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            
            # Создаем пароль
            await self._pool.execute("""
                INSERT INTO passwords (password, password_hash, created_by, duration_days, description, is_active)
                VALUES ($1, $2, $3, $4, $5, TRUE)
            """, password, password_hash, admin_id, days, description)
            
            # Создаем событие
            from actors.events.auth_events import PasswordCreatedEvent
            event = PasswordCreatedEvent.create(
                password=password,
                duration_days=days,
                description=description,
                created_by=admin_id
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            self.logger.info(f"Password '{password}' created by admin {admin_id}")
            
            return ADMIN_MESSAGES["password_created"].format(
                password=password,
                days=days,
                description=description
            )
            
        except Exception as e:
            self.logger.error(f"Error creating password: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))
        
    async def _admin_list_passwords(self, args: list) -> str:
        """Список всех паролей"""
        
        # Проверяем параметр full
        show_full = len(args) > 0 and args[0] == 'full'
        
        try:
            # Получаем все пароли
            passwords = await self._pool.fetch("""
                SELECT 
                    password,
                    description,
                    duration_days,
                    is_active,
                    created_at,
                    used_by,
                    expires_at
                FROM passwords
                ORDER BY created_at DESC
            """)
            
            if not passwords:
                return ADMIN_MESSAGES["passwords_empty"]
            
            # Формируем ответ
            lines = [ADMIN_MESSAGES["passwords_header"].format(count=len(passwords))]
            
            for i, pwd in enumerate(passwords, 1):
                # Маскируем пароль если не full
                if show_full:
                    display_password = pwd['password']
                else:
                    # Маскируем пароль
                    p = pwd['password']
                    if len(p) < 5:
                        display_password = f"{p[0]}***{p[-1]}" if len(p) > 1 else "***"
                    else:
                        display_password = f"{p[:2]}***{p[-2:]}"
                
                # Определяем статус
                if pwd['used_by']:
                    if pwd['expires_at']:
                        if pwd['expires_at'] > datetime.now(timezone.utc):
                            status = f"истекает {pwd['expires_at'].strftime('%d.%m')}"
                        else:
                            status = f"истек {pwd['expires_at'].strftime('%d.%m')}"
                    else:
                        status = "использован"
                else:
                    status = "не использован"
                
                # Выбираем шаблон
                template = ADMIN_MESSAGES["password_item_active" if pwd['is_active'] else "password_item_inactive"]
                
                lines.append(template.format(
                    index=i,
                    password=display_password,
                    description=pwd['description'],
                    days=pwd['duration_days'],
                    created=pwd['created_at'].strftime('%d.%m'),
                    status=status
                ))
            
            return "\n\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"Error listing passwords: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))
        
    async def _admin_deactivate_password(self, args: list, admin_id: str) -> str:
        """Деактивация пароля"""
        
        # Проверка аргументов
        if len(args) < 1:
            return ADMIN_MESSAGES["password_deactivate_usage"]
        
        password = args[0]
        
        try:
            # Проверяем существование и статус
            pwd_row = await self._pool.fetchrow("""
                SELECT is_active, used_by
                FROM passwords
                WHERE password = $1
            """, password)
            
            if not pwd_row:
                return ADMIN_MESSAGES["password_not_found"].format(password=password)
            
            if not pwd_row['is_active']:
                return ADMIN_MESSAGES["password_already_inactive"].format(password=password)
            
            # Деактивируем
            await self._pool.execute("""
                UPDATE passwords
                SET is_active = FALSE
                WHERE password = $1
            """, password)
            
            # Создаем событие
            from actors.events.auth_events import PasswordDeactivatedEvent
            event = PasswordDeactivatedEvent.create(
                password=password,
                deactivated_by=admin_id,  # берем из параметра метода
                was_used=pwd_row['used_by'] is not None,
                used_by=pwd_row['used_by']
            )
            await self._event_version_manager.append_event(event, self.get_actor_system())
            
            self.logger.info(f"Password '{password}' deactivated")
            
            return ADMIN_MESSAGES["password_deactivated"].format(password=password)
            
        except Exception as e:
            self.logger.error(f"Error deactivating password: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))
        
    async def _admin_stats(self) -> str:
        """Общая статистика системы"""
        
        try:
            # Статистика паролей
            password_stats = await self._pool.fetchrow("""
                SELECT 
                    COUNT(*) FILTER (WHERE is_active = TRUE) as active,
                    COUNT(*) FILTER (WHERE is_active = FALSE) as inactive,
                    COUNT(*) FILTER (WHERE used_by IS NOT NULL) as used
                FROM passwords
            """)
            
            # Статистика пользователей
            user_stats = await self._pool.fetchrow("""
                SELECT 
                    COUNT(DISTINCT user_id) as total_users
                FROM auth_attempts
            """)
            
            # Активные авторизации
            auth_stats = await self._pool.fetchrow("""
                SELECT COUNT(*) as authorized
                FROM authorized_users
                WHERE expires_at > CURRENT_TIMESTAMP
            """)
            
            # Заблокированные пользователи
            blocked_stats = await self._pool.fetchrow("""
                SELECT COUNT(*) as blocked
                FROM blocked_users
                WHERE blocked_until > CURRENT_TIMESTAMP
            """)
            
            # Группировка по длительности
            duration_stats = await self._pool.fetch("""
                SELECT duration_days, COUNT(*) as count
                FROM passwords
                GROUP BY duration_days
                ORDER BY duration_days
            """)
            
            # Активность за последние 24 часа
            activity_stats = await self._pool.fetchrow("""
                SELECT 
                    COUNT(*) as attempts,
                    COUNT(*) FILTER (WHERE success = TRUE) as success,
                    COUNT(*) FILTER (WHERE success = FALSE) as failed
                FROM auth_attempts
                WHERE timestamp > CURRENT_TIMESTAMP - INTERVAL '24 hours'
            """)
            
            # Формируем ответ
            lines = [ADMIN_MESSAGES["stats_header"]]
            
            # Пароли
            lines.append("\n" + ADMIN_MESSAGES["stats_passwords"].format(
                active=password_stats['active'] or 0,
                inactive=password_stats['inactive'] or 0,
                used=password_stats['used'] or 0
            ))
            
            # Пользователи
            lines.append("\n" + ADMIN_MESSAGES["stats_users"].format(
                total=user_stats['total_users'] or 0,
                authorized=auth_stats['authorized'] or 0,
                blocked=blocked_stats['blocked'] or 0
            ))
            
            # По длительности
            if duration_stats:
                duration_lines = []
                for stat in duration_stats:
                    duration_lines.append(f"• {stat['duration_days']} дней: {stat['count']} паролей")
                
                lines.append("\n" + ADMIN_MESSAGES["stats_by_duration"].format(
                    durations="\n".join(duration_lines)
                ))
            
            # Активность
            lines.append("\n" + ADMIN_MESSAGES["stats_recent_activity"].format(
                attempts=activity_stats['attempts'] or 0,
                success=activity_stats['success'] or 0,
                failed=activity_stats['failed'] or 0
            ))
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"Error generating stats: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))
        
    async def _admin_auth_log(self, args: list) -> str:
        """Просмотр логов авторизации"""
        
        # Проверяем параметр user_id
        user_filter = None
        filter_text = ""
        
        if len(args) > 0:
            user_id = args[0]
            # Проверяем формат user_id
            if not user_id.isdigit():
                return ADMIN_MESSAGES["auth_log_invalid_user"]
            user_filter = user_id
            filter_text = f" (user {user_id})"
        
        try:
            # Базовый запрос
            if user_filter:
                query = """
                    SELECT 
                        a.user_id,
                        a.password_attempt,
                        a.success,
                        a.error_reason,
                        a.timestamp,
                        p.duration_days
                    FROM auth_attempts a
                    LEFT JOIN passwords p ON a.password_attempt = p.password
                    WHERE a.user_id = $1
                    ORDER BY a.timestamp DESC
                    LIMIT 20
                """
                logs = await self._pool.fetch(query, user_filter)
            else:
                query = """
                    SELECT 
                        a.user_id,
                        a.password_attempt,
                        a.success,
                        a.error_reason,
                        a.timestamp,
                        p.duration_days
                    FROM auth_attempts a
                    LEFT JOIN passwords p ON a.password_attempt = p.password
                    ORDER BY a.timestamp DESC
                    LIMIT 20
                """
                logs = await self._pool.fetch(query)
            
            if not logs:
                return ADMIN_MESSAGES["auth_log_empty"].format(filter=filter_text)
            
            # Получаем информацию о блокировках
            blocked_users = {}
            if not user_filter:
                blocks = await self._pool.fetch("""
                    SELECT user_id, blocked_until, attempt_count
                    FROM blocked_users
                    WHERE blocked_until > CURRENT_TIMESTAMP
                """)
                blocked_users = {b['user_id']: b for b in blocks}
            
            # Формируем ответ
            lines = [ADMIN_MESSAGES["auth_log_header"].format(filter=filter_text)]
            
            for log in logs:
                time_str = log['timestamp'].strftime('%d.%m %H:%M')
                
                # Маскируем пароль
                pwd = log['password_attempt']
                if len(pwd) < 5:
                    masked_pwd = f"{pwd[0]}***{pwd[-1]}" if len(pwd) > 1 else "***"
                else:
                    masked_pwd = f"{pwd[:2]}***{pwd[-2:]}"
                
                if log['success']:
                    # Успешная авторизация
                    days = log['duration_days'] or "неизв."
                    lines.append("\n" + ADMIN_MESSAGES["auth_log_entry_success"].format(
                        time=time_str,
                        user_id=log['user_id'],
                        password=masked_pwd,
                        days=days
                    ))
                else:
                    # Неудачная попытка
                    reason_map = {
                        'invalid': 'неверный пароль',
                        'expired': 'пароль истек',
                        'deactivated': 'пароль деактивирован',
                        'already_used': 'пароль уже использован',
                        'blocked': 'пользователь заблокирован'
                    }
                    reason = reason_map.get(log['error_reason'], log['error_reason'] or 'неизвестно')
                    
                    lines.append("\n" + ADMIN_MESSAGES["auth_log_entry_failed"].format(
                        time=time_str,
                        user_id=log['user_id'],
                        password=masked_pwd,
                        reason=reason
                    ))
                
                # Проверяем блокировку
                if log['user_id'] in blocked_users and not user_filter:
                    block = blocked_users[log['user_id']]
                    seconds = int((block['blocked_until'] - datetime.now(timezone.utc)).total_seconds())
                    if seconds > 0:
                        lines.append(ADMIN_MESSAGES["auth_log_entry_blocked"].format(
                            time="",  # пустое время, так как это дополнительная информация
                            user_id=log['user_id'],
                            seconds=seconds
                        ))
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"Error getting auth log: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))
        
    async def _admin_blocked_users(self) -> str:
        """Список заблокированных пользователей"""
        
        try:
            # Получаем всех заблокированных пользователей
            blocked = await self._pool.fetch("""
                SELECT 
                    user_id,
                    blocked_until,
                    attempt_count,
                    last_attempt
                FROM blocked_users
                WHERE blocked_until > CURRENT_TIMESTAMP
                ORDER BY blocked_until DESC
            """)
            
            if not blocked:
                return ADMIN_MESSAGES["blocked_users_empty"]
            
            # Формируем ответ
            lines = [ADMIN_MESSAGES["blocked_users_header"].format(count=len(blocked))]
            
            for user in blocked:
                # Вычисляем оставшееся время
                now = datetime.now(timezone.utc)
                time_left_seconds = int((user['blocked_until'] - now).total_seconds())
                
                if time_left_seconds > 3600:
                    # Больше часа - показываем в часах и минутах
                    hours = time_left_seconds // 3600
                    minutes = (time_left_seconds % 3600) // 60
                    time_left = f"{hours}ч {minutes}мин"
                elif time_left_seconds > 60:
                    # Больше минуты - показываем в минутах
                    minutes = time_left_seconds // 60
                    time_left = f"{minutes} мин"
                else:
                    # Меньше минуты - показываем в секундах
                    time_left = f"{time_left_seconds} сек"
                
                # Форматируем время последней попытки
                last_attempt = user['last_attempt'].strftime('%d.%m %H:%M')
                
                lines.append("\n" + ADMIN_MESSAGES["blocked_user_entry"].format(
                    user_id=user['user_id'],
                    time_left=time_left,
                    attempts=user['attempt_count'],
                    last_attempt=last_attempt
                ))
            
            return "\n".join(lines)
            
        except Exception as e:
            self.logger.error(f"Error getting blocked users: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))
        
    async def _admin_unblock_user(self, args: list) -> str:
        """Разблокировка пользователя"""
        
        # Проверка аргументов
        if len(args) < 1:
            return ADMIN_MESSAGES["unblock_usage"]
        
        user_id = args[0]
        
        # Проверка формата user_id
        if not user_id.isdigit():
            return ADMIN_MESSAGES["unblock_invalid_user"]
        
        try:
            # Проверяем, заблокирован ли пользователь
            blocked = await self._pool.fetchrow("""
                SELECT blocked_until
                FROM blocked_users
                WHERE user_id = $1 AND blocked_until > CURRENT_TIMESTAMP
            """, user_id)
            
            if not blocked:
                return ADMIN_MESSAGES["unblock_not_blocked"].format(user_id=user_id)
            
            # Удаляем блокировку
            await self._pool.execute("""
                DELETE FROM blocked_users
                WHERE user_id = $1
            """, user_id)
            
            # Также удаляем недавние неудачные попытки для сброса счетчиков
            from config.settings import AUTH_ATTEMPTS_WINDOW
            cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=AUTH_ATTEMPTS_WINDOW)
            
            await self._pool.execute("""
                DELETE FROM auth_attempts
                WHERE user_id = $1 
                AND success = FALSE
                AND timestamp > $2
            """, user_id, cutoff_time)
            
            self.logger.info(f"User {user_id} unblocked by admin")
            
            # Создаем событие (опционально - можно добавить UnblockedByAdminEvent)
            # Но пока просто логируем действие
            
            return ADMIN_MESSAGES["unblock_success"].format(user_id=user_id)
            
        except Exception as e:
            self.logger.error(f"Error unblocking user {user_id}: {str(e)}", exc_info=True)
            return ADMIN_MESSAGES["command_error"].format(error=str(e))