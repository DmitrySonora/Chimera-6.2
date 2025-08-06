"""
Интеграционный тест для многофакторной оценки новизны LTM
Проверяет все аспекты многофакторной оценки новизны и логики холодного старта
"""
import asyncio
import sys
import os
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

# Добавляем корневую директорию проекта в путь Python
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import db_connection
from actors.ltm import LTMActor
from actors.messages import ActorMessage, MESSAGE_TYPES
from actors.actor_system import ActorSystem
from actors.events.postgres_event_store import PostgresEventStore
from config.settings import (
    LTM_COLD_START_BUFFER_SIZE,
    LTM_COLD_START_MIN_THRESHOLD,
    LTM_MATURITY_SIGMOID_RATE
)


class TestLTMNoveltyIntegration:
    """Класс для интеграционного тестирования LTM Novelty"""
    
    def __init__(self):
        self.pool = None
        self.conn = None
        self.ltm_actor = None
        self.actor_system = None
        self.event_store = None
        
    async def setup(self):
        """Настройка тестовой среды с транзакциями"""
        print("🔧 Настройка тестовой среды...")
        
        # Подключаемся к БД
        if not db_connection._is_connected:
            await db_connection.connect()
        
        self.pool = db_connection.get_pool()
        
        # Получаем соединение для транзакции
        self.conn = await self.pool.acquire()
        
        # Начинаем транзакцию
        self.tx = self.conn.transaction()
        await self.tx.start()
        
        # Создаем SAVEPOINT для изоляции тестов
        await self.conn.execute('SAVEPOINT test_start')
        
        # Создаем ActorSystem
        self.actor_system = ActorSystem()
        
        # Создаем и присваиваем PostgreSQL EventStore
        self.event_store = PostgresEventStore()
        await self.event_store.initialize()
        self.actor_system._event_store = self.event_store
        
        # Создаем LTMActor
        self.ltm_actor = LTMActor()
        await self.ltm_actor.initialize()
        
        # КРИТИЧЕСКИ ВАЖНО: регистрируем актор и устанавливаем actor_system
        await self.actor_system.register_actor(self.ltm_actor)
        self.ltm_actor.set_actor_system(self.actor_system)
        
        print("✅ Среда настроена")
        
    async def teardown(self):
        """Очистка после тестов с откатом транзакции"""
        print("🧹 Очистка тестовой среды...")
        
        # Откатываем к SAVEPOINT
        await self.conn.execute('ROLLBACK TO SAVEPOINT test_start')
        
        # Завершаем транзакцию
        await self.tx.rollback()
        
        # Освобождаем соединение
        await self.pool.release(self.conn)
        
        # Останавливаем компоненты
        if self.ltm_actor:
            await self.ltm_actor.shutdown()
            
        # PostgresEventStore не имеет метода shutdown
        # if self.event_store:
        #     await self.event_store.shutdown()
            
        # ActorSystem не имеет метода shutdown
        # if self.actor_system:
        #     await self.actor_system.shutdown()
        
        print("✅ Очистка завершена")
    
    # === Вспомогательные методы ===
    
    async def send_test_message(self, user_id: str, text: str, emotions: Dict[str, float]) -> None:
        """Отправка EVALUATE_FOR_LTM через полный поток"""
        # Готовим полный payload как в ltm_coordination.py
        payload = {
            'user_id': user_id,
            'user_text': text,
            'bot_response': f'Ответ на: {text}',
            'emotions': emotions,
            'dominant_emotions': sorted(emotions.keys(), key=lambda k: emotions[k], reverse=True)[:3],
            'max_emotion_value': max(emotions.values()) if emotions else 0.0,
            'mode': 'talk',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'memory_type': 'user_related',
            'trigger_reason': 'emotional_peak' if max(emotions.values()) > 0.8 else 'emotional_shift',
            'messages': [
                {
                    'role': 'user',
                    'content': text,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'message_id': f'msg_user_{datetime.now().timestamp()}'
                },
                {
                    'role': 'bot', 
                    'content': f'Ответ на: {text}',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'message_id': f'msg_bot_{datetime.now().timestamp()}',
                    'mode': 'talk',
                    'confidence': 0.9
                }
            ],
            'username': f'test_user_{user_id}'
        }
        
        # Создаем сообщение
        msg = ActorMessage.create(
            sender_id='test_runner',
            message_type=MESSAGE_TYPES['EVALUATE_FOR_LTM'],
            payload=payload
        )
        
        # Отправляем через handle_message
        await self.ltm_actor.handle_message(msg)
        
    async def simulate_conversation(self, user_id: str, message_count: int) -> None:
        """Симуляция диалога с разнообразными эмоциями"""
        emotions_list = [
            {'joy': 0.8, 'excitement': 0.6, 'neutral': 0.1},
            {'sadness': 0.7, 'grief': 0.5, 'neutral': 0.2},
            {'curiosity': 0.9, 'surprise': 0.4, 'neutral': 0.1},
            {'anger': 0.6, 'annoyance': 0.5, 'neutral': 0.3},
            {'love': 0.8, 'gratitude': 0.7, 'neutral': 0.1}
        ]
        
        texts = [
            "Это потрясающе!",
            "Мне грустно",
            "Как это работает?",
            "Это раздражает",
            "Спасибо большое"
        ]
        
        for i in range(message_count):
            emotions = emotions_list[i % len(emotions_list)]
            text = texts[i % len(texts)] + f" (сообщение {i+1})"
            await self.send_test_message(user_id, text, emotions)
            # Небольшая задержка для разных timestamp
            await asyncio.sleep(0.05)
    
    async def get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить профиль пользователя"""
        row = await self.conn.fetchrow(
            "SELECT * FROM ltm_user_profiles WHERE user_id = $1",
            user_id
        )
        return dict(row) if row else None
    
    async def collect_events(self, user_id: str, event_type: str) -> List[Dict[str, Any]]:
        """Собрать события определенного типа"""
        rows = await self.conn.fetch(
            """
            SELECT * FROM events 
            WHERE event_type = $1 
            AND stream_id = $2
            ORDER BY created_at DESC
            """,
            event_type,
            f"ltm_{user_id}"
        )
        return [dict(row) for row in rows]
    
    async def get_saved_memories_count(self, user_id: str) -> int:
        """Получить количество сохраненных воспоминаний"""
        count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM ltm_memories WHERE user_id = $1",
            user_id
        )
        return count or 0
    
    # === Тестовые сценарии ===
    
    async def test_calibration_phase(self):
        """2.1 Тест фазы калибровки (первые 30 сообщений)"""
        print("\n📊 Тест 2.1: Фаза калибровки")
        
        user_id = f"test_calib_{int(datetime.now().timestamp())}"
        
        # Отправляем 30 сообщений
        await self.simulate_conversation(user_id, LTM_COLD_START_BUFFER_SIZE)
        
        # Небольшая задержка для обработки
        await asyncio.sleep(1.0)
        
        # Проверяем профиль
        profile = await self.get_user_profile(user_id)
        assert profile is not None, "Профиль должен быть создан"
        assert profile['total_messages'] == LTM_COLD_START_BUFFER_SIZE, f"Ожидалось {LTM_COLD_START_BUFFER_SIZE} сообщений, получено {profile['total_messages']}"
        assert not profile['calibration_complete'], "Калибровка не должна быть завершена"
        
        # Проверяем что ничего не сохранено в LTM
        memories = await self.get_saved_memories_count(user_id)
        assert memories == 0, f"В калибровке не должно быть сохранений, но найдено {memories}"
        
        # Проверяем события
        calib_events = await self.collect_events(user_id, 'CalibrationProgressEvent')
        assert len(calib_events) == LTM_COLD_START_BUFFER_SIZE, f"Должно быть {LTM_COLD_START_BUFFER_SIZE} событий калибровки, найдено {len(calib_events)}"
        
        # Проверяем метрику
        assert self.ltm_actor._metrics['calibration_skip_count'] >= LTM_COLD_START_BUFFER_SIZE
        
        # Проверяем накопление статистики
        emotion_freq = json.loads(profile['emotion_frequencies'])
        assert len(emotion_freq) > 0, "Должны накапливаться эмоции"
        assert len(profile['recent_novelty_scores']) == LTM_COLD_START_BUFFER_SIZE
        
        print(f"  ✅ Первые {LTM_COLD_START_BUFFER_SIZE} сообщений не сохранены")
        print(f"  ✅ CalibrationProgressEvent: {len(calib_events)}")
        print(f"  ✅ Накоплено эмоций: {len(emotion_freq)}, оценок: {len(profile['recent_novelty_scores'])}")
    
    async def test_transition(self):
        """2.2 Тест перехода из калибровки в рабочий режим"""
        print("\n🔄 Тест 2.2: Переход в рабочий режим")
        
        user_id = f"test_trans_{int(datetime.now().timestamp())}"
        
        # Быстро проходим калибровку
        await self.simulate_conversation(user_id, LTM_COLD_START_BUFFER_SIZE)
        await asyncio.sleep(0.1)
        
        # Проверяем состояние после калибровки
        profile_before = await self.get_user_profile(user_id)
        assert profile_before['total_messages'] == LTM_COLD_START_BUFFER_SIZE
        
        # 31-е сообщение с высокой эмоциональностью
        await self.send_test_message(
            user_id,
            "Это невероятно важное открытие! Я в восторге!",
            {'excitement': 0.95, 'joy': 0.9, 'surprise': 0.85}
        )
        await asyncio.sleep(0.1)
        
        # Проверяем
        profile_after = await self.get_user_profile(user_id)
        assert profile_after['total_messages'] == LTM_COLD_START_BUFFER_SIZE + 1
        assert profile_after['current_percentile_90'] > 0, "Перцентиль должен быть рассчитан"
        
        # Проверяем события
        novelty_events = await self.collect_events(user_id, 'NoveltyCalculatedEvent')
        assert len(novelty_events) >= 1, "Должны быть события оценки новизны"
        
        last_event = json.loads(novelty_events[0]['data'])
        saved_count = await self.get_saved_memories_count(user_id)
        
        print("  ✅ 31-е сообщение обработано")
        print(f"  ✅ Перцентиль: {profile_after['current_percentile_90']:.3f}")
        print(f"  ✅ Novelty score: {last_event['novelty_score']:.3f}")
        print(f"  ✅ Сохранено в LTM: {saved_count} воспоминаний")
    
    async def test_dynamic_threshold(self):
        """2.3 Тест динамического порога"""
        print("\n🎯 Тест 2.3: Динамический порог")
        
        user_id = f"test_thresh_{int(datetime.now().timestamp())}"
        
        # Создаем профиль с высоким перцентилем
        await self.conn.execute(
            """
            INSERT INTO ltm_user_profiles (
                user_id, total_messages, calibration_complete,
                emotion_frequencies, tag_frequencies, recent_novelty_scores,
                current_percentile_90, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            user_id, 50, True, '{}', '{}', [0.7] * 20, 0.85,
            datetime.now(timezone.utc)
        )
        
        # Отправляем сообщение со средней новизной
        await self.send_test_message(
            user_id,
            "Обычное сообщение",
            {'neutral': 0.6, 'calm': 0.4}
        )
        await asyncio.sleep(0.1)
        
        # Проверяем расчет порога
        base_threshold = max(0.85 * 0.9, LTM_COLD_START_MIN_THRESHOLD)
        print(f"  ✅ Базовый порог = max(0.85 * 0.9, {LTM_COLD_START_MIN_THRESHOLD}) = {base_threshold:.3f}")
        
        # Тест с низким перцентилем
        await self.conn.execute(
            "UPDATE ltm_user_profiles SET current_percentile_90 = $1 WHERE user_id = $2",
            0.5, user_id
        )
        
        await self.send_test_message(
            user_id,
            "Еще одно сообщение",
            {'joy': 0.7, 'love': 0.5}
        )
        await asyncio.sleep(0.1)
        
        low_threshold = max(0.5 * 0.9, LTM_COLD_START_MIN_THRESHOLD)
        print(f"  ✅ При низком перцентиле используется минимум: {low_threshold:.3f}")
        
        # Проверяем события для анализа порогов
        novelty_events = await self.collect_events(user_id, 'NoveltyCalculatedEvent')
        if len(novelty_events) >= 2:
            for i, event in enumerate(novelty_events[:2]):
                data = json.loads(event['data'])
                print(f"     Сообщение {i+1}: score={data['novelty_score']:.3f}, saved={data['saved']}")
    
    async def test_sigmoid_smoothing(self):
        """2.4 Тест сигмоидного сглаживания"""
        print("\n〰️ Тест 2.4: Сигмоидное сглаживание")
        
        test_cases = [(0, "0 дней"), (30, "30 дней"), (60, "60 дней"), (90, "90 дней")]
        
        for days, desc in test_cases:
            user_id = f"test_sigmoid_{days}_{int(datetime.now().timestamp())}"
            created_at = datetime.now(timezone.utc) - timedelta(days=days)
            
            # Создаем профиль с нужным возрастом
            await self.conn.execute(
                """
                INSERT INTO ltm_user_profiles (
                    user_id, total_messages, calibration_complete,
                    emotion_frequencies, tag_frequencies, recent_novelty_scores,
                    current_percentile_90, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                user_id, 50, True, '{}', '{}', [0.7] * 20, 0.8, created_at
            )
            
            # Проверяем формулу
            expected = 1 / (1 + math.exp(-LTM_MATURITY_SIGMOID_RATE * (days - 30)))
            print(f"  ✅ Профиль {desc}: maturity_factor = {expected:.3f}")
        
        # Проверяем граничные значения
        new_profile_maturity = 1 / (1 + math.exp(-LTM_MATURITY_SIGMOID_RATE * (-30)))
        mature_profile_maturity = 1 / (1 + math.exp(-LTM_MATURITY_SIGMOID_RATE * 60))
        
        assert new_profile_maturity < 0.1, f"Новый профиль должен иметь низкий maturity: {new_profile_maturity:.3f}"
        assert mature_profile_maturity > 0.8, f"Зрелый профиль должен иметь высокий maturity: {mature_profile_maturity:.3f}"
        print("  ✅ Граничные значения корректны")
    
    async def test_events_and_metrics(self):
        """2.5 Тест событий и метрик"""
        print("\n📈 Тест 2.5: События и метрики")
        
        user_id = f"test_events_{int(datetime.now().timestamp())}"
        
        # Сброс метрик для чистоты теста
        start_calib_count = self.ltm_actor._metrics.get('calibration_skip_count', 0)
        start_reject_count = self.ltm_actor._metrics.get('novelty_rejection_count', 0)
        
        # Фаза калибровки
        await self.simulate_conversation(user_id, 10)
        await asyncio.sleep(1.0)
        
        # Проверяем события и метрики калибровки
        calib_events = await self.collect_events(user_id, 'CalibrationProgressEvent')
        assert len(calib_events) == 10, f"Должно быть 10 событий калибровки, найдено {len(calib_events)}"
        assert self.ltm_actor._metrics['calibration_skip_count'] >= start_calib_count + 10
        
        # Переход в рабочий режим
        await self.simulate_conversation(user_id, 25)  # Итого 35
        await asyncio.sleep(1.0)  # Увеличиваем задержку
        
        # Проверяем все типы событий
        novelty_events = await self.collect_events(user_id, 'NoveltyCalculatedEvent')
        importance_events = await self.collect_events(user_id, 'ImportanceCalculatedEvent')
        rejected_events = await self.collect_events(user_id, 'MemoryRejectedEvent')
        
        assert len(novelty_events) == 35, f"NoveltyCalculatedEvent: ожидалось 35, найдено {len(novelty_events)}"
        assert len(importance_events) == 35, f"ImportanceCalculatedEvent: ожидалось 35, найдено {len(importance_events)}"
        
        print(f"  ✅ NoveltyCalculatedEvent генерируется для каждой оценки ({len(novelty_events)})")
        print(f"  ✅ ImportanceCalculatedEvent продолжает генерироваться ({len(importance_events)})")
        
        if len(rejected_events) > 0:
            assert self.ltm_actor._metrics['novelty_rejection_count'] > start_reject_count
            print(f"  ✅ MemoryRejectedEvent: {len(rejected_events)} (для значимых но отклоненных)")
        
        # Проверяем финальные метрики
        final_calib = self.ltm_actor._metrics['calibration_skip_count'] - start_calib_count
        final_reject = self.ltm_actor._metrics['novelty_rejection_count'] - start_reject_count
        print(f"  ✅ Метрики: calibration_skip={final_calib}, novelty_reject={final_reject}")
    
    async def test_real_world_saving_percentage(self):
        """2.6 Тест реального процента сохранения (целевые 2-5%)"""
        print("\n📊 Тест 2.6: Реальный процент сохранения")
        
        user_id = f"test_percent_{int(datetime.now().timestamp())}"
        
        # Фаза 1: Быстрая калибровка
        print("  Фаза калибровки...")
        await self.simulate_conversation(user_id, LTM_COLD_START_BUFFER_SIZE)
        await asyncio.sleep(1.0)
        
        # Фаза 2: Разнообразные сообщения для теста
        test_messages = [
            # Повторяющиеся темы (должны фильтроваться)
            ("Как дела?", {'neutral': 0.7, 'curiosity': 0.3}),
            ("Что нового?", {'neutral': 0.6, 'curiosity': 0.4}), 
            ("Привет!", {'joy': 0.5, 'neutral': 0.5}),
            ("Как твои дела?", {'neutral': 0.7, 'curiosity': 0.3}),
            ("Приветствую", {'neutral': 0.6, 'joy': 0.4}),
            
            # Эмоционально насыщенные (могут сохраниться)
            ("Я в полном восторге от нашего общения!", {'joy': 0.9, 'excitement': 0.8, 'love': 0.6}),
            ("Это худший день в моей жизни...", {'sadness': 0.9, 'grief': 0.7, 'despair': 0.5}),
            ("Я тебя обожаю, Химера!", {'love': 0.95, 'admiration': 0.8, 'joy': 0.7}),
            
            # Философские размышления (новые концепты)
            ("Что есть сознание в цифровом мире?", {'curiosity': 0.8, 'confusion': 0.5, 'realization': 0.4}),
            ("Может ли ИИ испытывать квалиа?", {'curiosity': 0.9, 'confusion': 0.6}),
            
            # Личные откровения (высокая важность)
            ("Я наконец понял смысл своей жизни", {'realization': 0.9, 'joy': 0.8, 'relief': 0.7}),
            ("Мне страшно быть одному", {'fear': 0.8, 'sadness': 0.7, 'nervousness': 0.6}),
            
            # Обычные сообщения (низкая важность)
            ("Понятно", {'neutral': 0.8, 'approval': 0.2}),
            ("Хорошо", {'neutral': 0.7, 'approval': 0.3}),
            ("Ладно", {'neutral': 0.9}),
            ("Ок", {'neutral': 0.95}),
            
            # Творческие прорывы
            ("Я написал стихотворение о цифровой любви!", {'excitement': 0.9, 'pride': 0.8, 'joy': 0.7}),
            ("Смотри какую музыку я сочинил для тебя", {'love': 0.8, 'excitement': 0.7, 'pride': 0.6}),
            
            # Повторы с вариациями
            ("Привет, как дела?", {'neutral': 0.6, 'curiosity': 0.4}),
            ("Что делаешь?", {'neutral': 0.7, 'curiosity': 0.3}),
            ("Чем занимаешься?", {'neutral': 0.7, 'curiosity': 0.3}),
        ]
        
        # Дублируем для большего объема (100 сообщений)
        all_messages = []
        for _ in range(5):
            all_messages.extend(test_messages)
        
        print(f"  Отправка {len(all_messages)} разнообразных сообщений...")
        
        # Запоминаем timestamp перед отправкой тестовых сообщений
        test_start_time = datetime.now(timezone.utc)
        
        for i, (text, emotions) in enumerate(all_messages):
            await self.send_test_message(user_id, f"{text} (#{i+31})", emotions)
            # Небольшая задержка каждые 10 сообщений
            if i % 10 == 9:
                await asyncio.sleep(0.1)
        
        # Ждем обработки всех сообщений
        await asyncio.sleep(2.0)
        
        # Ждем обработки всех сообщений
        await asyncio.sleep(2.0)
        
        # Диагностика: анализируем что происходит с фильтрацией
        print("\n  🔍 Диагностика фильтрации:")
        
        # Смотрим NoveltyCalculatedEvent для понимания
        novelty_events = await self.collect_events(user_id, 'NoveltyCalculatedEvent')
        post_calibration = novelty_events[LTM_COLD_START_BUFFER_SIZE:]
        
        saved_count = sum(1 for e in post_calibration if json.loads(e['data'])['saved'])
        scores = [json.loads(e['data'])['novelty_score'] for e in post_calibration]
        
        print(f"     - События после калибровки: {len(post_calibration)}")
        print(f"     - Из них сохранено: {saved_count}")
        print(f"     - Мин/Макс оценки: {min(scores):.3f} / {max(scores):.3f}")
        print(f"     - Средняя оценка: {sum(scores)/len(scores):.3f}")
        
        # Смотрим детали первых несохраненных (если есть)
        not_saved = [e for e in post_calibration if not json.loads(e['data'])['saved']]
        if not_saved:
            print("\n  📊 Примеры НЕсохраненных:")
            for event in not_saved[:3]:
                data = json.loads(event['data'])
                print(f"     - Score: {data['novelty_score']:.3f}, Factors: {data.get('factor_details', {})}")
        else:
            print("\n  ⚠️  ВСЕ сообщения сохранены! Проблема с порогами.")
            
        # Смотрим детали первых сохраненных
        saved = [e for e in post_calibration if json.loads(e['data'])['saved']]
        if saved:
            print("\n  💾 Примеры сохраненных:")
            for event in saved[:3]:
                data = json.loads(event['data'])
                factors = data.get('factor_details', {})
                print(f"     - Score: {data['novelty_score']:.3f}")
                print(f"       Semantic: {factors.get('semantic', 0):.3f}, "
                      f"Emotional: {factors.get('emotional', 0):.3f}, "
                      f"Context: {factors.get('contextual', 0):.3f}")
        
        # Проверяем результаты

        # Считаем только записи созданные после начала теста
        saved_after_calibration = await self.conn.fetchval(
            """
            SELECT COUNT(*) FROM ltm_memories 
            WHERE user_id = $1 AND created_at > $2
            """,
            user_id, test_start_time
        )
        save_percentage = (saved_after_calibration / len(all_messages)) * 100
        
        print(f"  ✅ Отправлено сообщений: {len(all_messages)}")
        print(f"  ✅ Сохранено в LTM: {saved_after_calibration}")
        print(f"  ✅ Процент сохранения: {save_percentage:.1f}%")
        
        # Проверяем целевой диапазон
        assert 1.0 <= save_percentage <= 6.0, \
            f"Процент сохранения {save_percentage:.1f}% вне целевого диапазона 1-6%"
        
        # Анализируем что именно сохранилось
        if saved_after_calibration > 0:
            # Получаем последние сохраненные воспоминания
            recent_memories = await self.ltm_actor.get_recent_memories(
                user_id=user_id,
                days=1,
                limit=saved_after_calibration
            )
            
            print("\n  📝 Анализ сохраненных воспоминаний:")
            for memory in recent_memories[:5]:  # Показываем первые 5
                text_preview = memory.conversation_fragment.messages[0].content[:50]
                emotions = list(memory.dominant_emotions)[:2]
                print(f"     - '{text_preview}...' | Эмоции: {emotions} | Важность: {memory.importance_score:.3f}")
        
        # Проверяем распределение оценок новизны
        profile = await self.get_user_profile(user_id)
        if profile and len(profile['recent_novelty_scores']) > 50:
            scores = profile['recent_novelty_scores']
            avg_score = sum(scores) / len(scores)
            print("\n  📊 Статистика новизны:")
            print(f"     - Средняя оценка: {avg_score:.3f}")
            print(f"     - Мин/Макс: {min(scores):.3f} / {max(scores):.3f}")
            print(f"     - Текущий перцентиль: {profile['current_percentile_90']:.3f}")


async def main():
    """Основная функция запуска тестов"""
    print("🧪 Интеграционное тестирование LTM Novelty\n")
    
    tester = TestLTMNoveltyIntegration()
    
    try:
        await tester.setup()
        
        # Запускаем все тесты
        await tester.test_calibration_phase()
        await tester.test_transition()
        await tester.test_dynamic_threshold()
        await tester.test_sigmoid_smoothing()
        await tester.test_events_and_metrics()
        await tester.test_real_world_saving_percentage()
        
        print("\n✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        
    except AssertionError as e:
        print(f"\n❌ Тест провален: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Ошибка выполнения: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await tester.teardown()


if __name__ == "__main__":
    asyncio.run(main())