"""
Вспомогательные методы для AuthActor
"""
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta


class AuthHelpers:
    """Миксин с вспомогательными методами для AuthActor"""
    
    # Эти атрибуты будут доступны из AuthActor
    _pool: Optional[object]
    _redis_connection: Optional[object]
    _event_version_manager: object
    _auth_circuit_breakers: dict
    _metrics: dict
    logger: object
    
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
    
    async def _check_user_blocked(self, user_id: str) -> Tuple[bool, Optional[datetime]]:
        """
        Проверяет, заблокирован ли пользователь.
        
        Returns:
            (is_blocked, blocked_until) - кортеж из флага и времени разблокировки
        """
        try:
            query = """
                SELECT blocked_until 
                FROM blocked_users 
                WHERE user_id = $1 AND blocked_until > CURRENT_TIMESTAMP
            """
            
            row = await self._pool.fetchrow(query, user_id)
            
            if row:
                blocked_until = row['blocked_until'].replace(tzinfo=timezone.utc)
                self.logger.info(f"Blocked user {user_id} tried to authenticate")
                return True, blocked_until
            
            return False, None
            
        except Exception as e:
            self.logger.error(f"Error checking user block status: {str(e)}")
            self._increment_metric('db_errors')
            return False, None  # При ошибке БД разрешаем попытку
    
    async def _increment_failed_attempts(self, user_id: str) -> int:
        """
        Подсчитывает количество неудачных попыток за последние 15 минут.
        
        Returns:
            Текущее количество неудачных попыток
        """
        try:
            from config.settings import AUTH_ATTEMPTS_WINDOW
            
            query = """
                SELECT COUNT(*) as count
                FROM auth_attempts 
                WHERE user_id = $1 
                  AND success = FALSE 
                  AND timestamp > CURRENT_TIMESTAMP - INTERVAL '%s seconds'
            """ % AUTH_ATTEMPTS_WINDOW
            
            count = await self._pool.fetchval(query, user_id)
            
            self.logger.debug(f"User {user_id} has {count} failed attempts in last {AUTH_ATTEMPTS_WINDOW} seconds")
            
            return count or 0
            
        except Exception as e:
            self.logger.error(f"Error counting failed attempts: {str(e)}")
            self._increment_metric('db_errors')
            return 0  # При ошибке БД возвращаем 0
    
    async def _block_user(self, user_id: str, attempt_count: int) -> None:
        """
        Блокирует пользователя на 15 минут.
        
        Args:
            user_id: ID пользователя
            attempt_count: Количество попыток для записи
        """
        try:
            from config.settings import AUTH_BLOCK_DURATION
            
            blocked_until = datetime.now(timezone.utc) + timedelta(seconds=AUTH_BLOCK_DURATION)
            
            query = """
                INSERT INTO blocked_users (user_id, blocked_until, attempt_count, last_attempt)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE 
                SET blocked_until = $2,
                    attempt_count = $3,
                    last_attempt = CURRENT_TIMESTAMP
            """
            
            await self._pool.execute(query, user_id, blocked_until, attempt_count)
            
            self.logger.warning(f"User {user_id} blocked after {attempt_count} failed attempts")
            
            # Создаем событие блокировки
            from actors.events.auth_events import BlockedUserEvent
            blocked_event = BlockedUserEvent.create(
                user_id=user_id,
                blocked_until=blocked_until,
                attempt_count=attempt_count
            )
            await self._event_version_manager.append_event(blocked_event, self.get_actor_system())
            
            # Создаем событие брутфорса
            from actors.events import BruteforceDetectedEvent
            bruteforce_event = BruteforceDetectedEvent.create(
                user_id=user_id,
                attempts_count=attempt_count,
                action_taken="user_blocked"
            )
            await self._event_version_manager.append_event(bruteforce_event, self.get_actor_system())
            
            # Помечаем Circuit Breaker как открытый
            from utils.circuit_breaker import CircuitState
            import time
            if user_id in self._auth_circuit_breakers:
                self._auth_circuit_breakers[user_id]._state = CircuitState.OPEN
                self._auth_circuit_breakers[user_id]._last_failure_time = time.time()
            
            # Обновляем метрику
            self._metrics['blocked_users_count'] += 1
            
        except Exception as e:
            self.logger.error(f"Error blocking user: {str(e)}")
            self._increment_metric('db_errors')
    
    async def _reset_daily_counters(self) -> None:
        """Сброс дневных счетчиков сообщений"""
        if not self._redis_connection or not self._redis_connection.is_connected():
            self.logger.warning("Cannot reset daily counters - Redis not connected")
            return
            
        try:
            # Получаем клиент Redis
            client = self._redis_connection.get_client()
            if not client:
                return
                
            # Формируем паттерн для ключей текущего дня
            from datetime import date
            today = date.today().isoformat()
            pattern = self._redis_connection.make_key("daily_limit", "*", today)
            
            # Получаем все ключи за сегодня
            keys = []
            async for key in client.scan_iter(match=pattern):
                keys.append(key)
            
            # Удаляем ключи батчами
            if keys:
                deleted = await client.delete(*keys)
                self.logger.info(f"Daily reset: deleted {deleted} limit counters")
                
                # Создаем событие
                from actors.events import BaseEvent
                reset_event = BaseEvent.create(
                    stream_id="auth_system",
                    event_type="DailyCountersResetEvent",
                    data={
                        "date": today,
                        "counters_deleted": deleted,
                        "reset_at": datetime.now().isoformat()
                    }
                )
                await self._event_version_manager.append_event(reset_event, self.get_actor_system())
            else:
                self.logger.debug("Daily reset: no counters to reset")
                
        except Exception as e:
            self.logger.error(f"Error in daily reset: {str(e)}")
            self._increment_metric('db_errors')
    
    async def _get_daily_message_count(self, user_id: str) -> Optional[int]:
        """
        Получить количество сообщений пользователя за сегодня из Redis.
        
        Returns:
            Количество сообщений или None если Redis недоступен
        """
        if not self._redis_connection or not self._redis_connection.is_connected():
            return None
            
        try:
            # Формируем ключ с текущей датой
            from datetime import date
            today = date.today().isoformat()
            key = self._redis_connection.make_key("daily_limit", user_id, today)
            
            # Получаем значение
            value = await self._redis_connection.get(key)
            return int(value) if value else 0
            
        except Exception as e:
            self.logger.error(f"Failed to get message count for user {user_id}: {str(e)}")
            return None
    
    async def _increment_daily_message_count(self, user_id: str) -> Optional[int]:
        """
        Увеличить счетчик сообщений пользователя на 1.
        
        Returns:
            Новое значение счетчика или None если Redis недоступен
        """
        if not self._redis_connection or not self._redis_connection.is_connected():
            return None
            
        try:
            # Формируем ключ с текущей датой
            from datetime import date
            today = date.today().isoformat()
            key = self._redis_connection.make_key("daily_limit", user_id, today)
            
            # Инкрементируем с TTL 24 часа
            from config.settings import REDIS_DAILY_LIMIT_TTL
            new_value = await self._redis_connection.increment(key)
            
            # Устанавливаем TTL только при первом инкременте
            if new_value == 1:
                client = self._redis_connection.get_client()
                if client:
                    await client.expire(key, REDIS_DAILY_LIMIT_TTL)
            
            self.logger.debug(f"Incremented message count for user {user_id}: {new_value}")
            return new_value
            
        except Exception as e:
            self.logger.error(f"Failed to increment message count for user {user_id}: {str(e)}")
            return None
    
    async def _reset_daily_message_count(self, user_id: str) -> bool:
        """
        Сбросить счетчик сообщений пользователя (для админских команд).
        
        Returns:
            True если успешно, False если ошибка
        """
        if not self._redis_connection or not self._redis_connection.is_connected():
            return False
            
        try:
            # Формируем ключ с текущей датой
            from datetime import date
            today = date.today().isoformat()
            key = self._redis_connection.make_key("daily_limit", user_id, today)
            
            # Удаляем ключ
            await self._redis_connection.delete(key)
            self.logger.info(f"Reset message count for user {user_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to reset message count for user {user_id}: {str(e)}")
            return False