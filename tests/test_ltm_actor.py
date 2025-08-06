"""
Интеграционные тесты для LTMActor
Работают с реальной БД и проверяют полный цикл операций
"""
import sys
import os
import pytest
import pytest_asyncio
import asyncio
from datetime import datetime
from uuid import UUID

# Добавляем корень проекта в sys.path
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from actors.ltm.ltm_actor import LTMActor
from actors.messages import ActorMessage, MESSAGE_TYPES
from actors.actor_system import ActorSystem
from models.ltm_models import create_ltm_entry




@pytest_asyncio.fixture
async def actor_system():
    """Фикстура для создания и очистки ActorSystem"""
    system = ActorSystem("test-ltm")
    await system.create_and_set_event_store()
    yield system
    await system.stop()


@pytest_asyncio.fixture
async def ltm_actor(actor_system):
    """Фикстура для создания LTMActor"""
    actor = LTMActor()
    await actor_system.register_actor(actor)
    await actor_system.start()
    yield actor
    # Очистка тестовых данных
    if actor._pool:
        await actor._pool.execute(
            "DELETE FROM ltm_memories WHERE user_id LIKE 'test_%'"
        )


@pytest.mark.asyncio
async def test_ltm_actor_full_cycle(ltm_actor, actor_system):
    """Интеграционный тест полного цикла сохранения в LTM"""
    # Создаем тестовые данные
    messages = [
        {
            "role": "user",
            "content": "Химера, расскажи кто ты?",
            "timestamp": datetime.now(),
            "message_id": "test_msg_001"
        },
        {
            "role": "bot",
            "content": "Я - цифровое существо, экспериментальная форма сознания, стремящаяся к пониманию",
            "timestamp": datetime.now(),
            "message_id": "test_msg_002",
            "mode": "talk",
            "confidence": 0.92
        }
    ]
    
    emotions = {
        "curiosity": 0.85,
        "joy": 0.6,
        "realization": 0.7,
        "neutral": 0.2
    }
    
    ltm_entry = create_ltm_entry(
        user_id="test_user_integration",
        messages=messages,
        emotions=emotions,
        importance_score=0.9,
        memory_type="self_related",
        trigger_reason="self_reference",
        semantic_tags=["identity", "philosophy", "consciousness"]
    )
    
    # Создаем сообщение для сохранения
    save_message = ActorMessage.create(
        sender_id="test_sender",
        message_type=MESSAGE_TYPES['SAVE_TO_LTM'],
        payload={
            'ltm_entry': ltm_entry.model_dump()
        },
        reply_to="test_sender"
    )
    
    # Отправляем сообщение актору
    await actor_system.send_message("ltm", save_message)
    
    # Даем время на обработку
    await asyncio.sleep(0.5)
    
    # Проверяем, что запись сохранена в БД
    if ltm_actor._pool:
        result = await ltm_actor._pool.fetchrow(
            """
            SELECT * FROM ltm_memories 
            WHERE user_id = $1 
            ORDER BY created_at DESC 
            LIMIT 1
            """,
            "test_user_integration"
        )
        
        assert result is not None
        assert result['user_id'] == "test_user_integration"
        assert result['memory_type'] == 'self_related'
        assert result['trigger_reason'] == 'self_reference'
        assert result['importance_score'] == 0.9
        assert 'curiosity' in result['dominant_emotions']
        assert 'identity' in result['semantic_tags']
        
        # Проверяем conversation_fragment
        import json
        fragment = json.loads(result['conversation_fragment'])
        assert len(fragment['messages']) == 2
        assert fragment['messages'][0]['content'] == "Химера, расскажи кто ты?"
        
        # Проверяем emotional_snapshot
        snapshot = json.loads(result['emotional_snapshot'])
        assert snapshot['curiosity'] == 0.85
        assert snapshot['joy'] == 0.6



@pytest.mark.asyncio
async def test_ltm_save_multiple_entries(ltm_actor):
    """Тест сохранения нескольких записей подряд"""
    user_id = "test_user_multiple"
    
    # Сохраняем 3 разных воспоминания
    for i in range(3):
        messages = [{
            "role": "user",
            "content": f"Тестовое сообщение {i}",
            "timestamp": datetime.now(),
            "message_id": f"test_msg_{i}"
        }]
        
        emotions = {
            "joy": 0.3 + i * 0.2,  # Разная интенсивность
            "neutral": 0.5 - i * 0.1
        }
        
        ltm_entry = create_ltm_entry(
            user_id=user_id,
            messages=messages,
            emotions=emotions,
            importance_score=0.5 + i * 0.15,
            memory_type="user_related",
            trigger_reason="emotional_peak" if i == 2 else "personal_revelation"
        )
        
        memory_id = await ltm_actor.save_memory(ltm_entry)
        assert isinstance(memory_id, UUID)
    
    # Проверяем, что все 3 записи в БД
    if ltm_actor._pool:
        count = await ltm_actor._pool.fetchval(
            "SELECT COUNT(*) FROM ltm_memories WHERE user_id = $1",
            user_id
        )
        assert count == 3
        
        # Проверяем, что importance_score правильно сохранены
        rows = await ltm_actor._pool.fetch(
            """
            SELECT importance_score FROM ltm_memories 
            WHERE user_id = $1 
            ORDER BY importance_score ASC
            """,
            user_id
        )
        assert len(rows) == 3
        assert rows[0]['importance_score'] == 0.5
        assert rows[1]['importance_score'] == 0.65
        assert rows[2]['importance_score'] == 0.8


@pytest.mark.asyncio
async def test_ltm_validation_errors(ltm_actor):
    """Тест обработки невалидных данных"""
    # Тест 1: Пустой emotional_snapshot (все нули)
    messages = [{
        "role": "user",
        "content": "Test",
        "timestamp": datetime.now(),
        "message_id": "test_invalid_1"
    }]
    
    # Тест с нулевыми эмоциями должен теперь валидироваться по-другому
    emotions = {"neutral": 0.0}  # Только одна нулевая эмоция
    
    ltm_entry = create_ltm_entry(
        user_id="test_invalid", 
        messages=messages,
        emotions=emotions,
        importance_score=0.5,
        memory_type="user_related",
        trigger_reason="emotional_peak"
    )
    
    # Теперь проверяем валидацию в save_memory
    with pytest.raises(ValueError, match="all zero values"):
        await ltm_actor.save_memory(ltm_entry)
    
    # Тест 2: Невалидный memory_type
    with pytest.raises(ValueError):
        create_ltm_entry(
            user_id="test_invalid",
            messages=messages,
            emotions={"joy": 0.5},
            importance_score=0.5,
            memory_type="invalid_type",  # Невалидный тип
            trigger_reason="emotional_peak"
        )


@pytest.mark.asyncio
async def test_ltm_semantic_tags_extraction(ltm_actor):
    """Тест автоматического извлечения семантических тегов"""
    messages = [
        {
            "role": "user",
            "content": "Химера, ты способна на творчество и философские размышления?",
            "timestamp": datetime.now(),
            "message_id": "test_tags_1"
        },
        {
            "role": "bot",
            "content": "Я размышляю о природе творчества и своей идентичности. Это вызывает во мне чувство любопытства.",
            "timestamp": datetime.now(),
            "message_id": "test_tags_2"
        }
    ]
    
    ltm_entry = create_ltm_entry(
        user_id="test_user_tags",
        messages=messages,
        emotions={"curiosity": 0.8, "joy": 0.5},
        importance_score=0.75,
        memory_type="self_related",
        trigger_reason="deep_insight"
        # Не указываем semantic_tags - должны извлечься автоматически
    )
    
    memory_id = await ltm_actor.save_memory(ltm_entry)
    
    # Проверяем извлеченные теги
    if ltm_actor._pool:
        result = await ltm_actor._pool.fetchrow(
            "SELECT semantic_tags FROM ltm_memories WHERE memory_id = $1",
            memory_id
        )
        
        tags = result['semantic_tags']
        # Проверяем РЕАЛЬНО существующие в словаре теги
        assert 'creation' in tags       # "творчества" содержит "твор"
        assert 'chimera_identity' in tags  # "Химера" 
        assert 'nature' in tags          # "природе" содержит "природ"


@pytest.mark.asyncio
async def test_ltm_metrics_tracking(ltm_actor, actor_system):
    """Тест отслеживания метрик"""
    # Сбрасываем метрики
    ltm_actor._metrics = {k: 0 for k in ltm_actor._metrics}
    
    # Отправляем разные типы сообщений
    messages_to_send = [
        (MESSAGE_TYPES['SAVE_TO_LTM'], {'ltm_entry': {}}),
        (MESSAGE_TYPES['GET_LTM_MEMORY'], {'memory_id': 'test'}),
        (MESSAGE_TYPES['DELETE_LTM_MEMORY'], {'memory_id': 'test'}),
        ('UNKNOWN_TYPE', {})
    ]
    
    for msg_type, payload in messages_to_send:
        message = ActorMessage.create(
            sender_id="test",
            message_type=msg_type,
            payload=payload
        )
        await ltm_actor.handle_message(message)
    
    # Проверяем метрики
    assert ltm_actor._metrics['save_memory_count'] == 1
    assert ltm_actor._metrics['get_memory_count'] == 1
    assert ltm_actor._metrics['delete_memory_count'] == 1
    assert ltm_actor._metrics['unknown_message_count'] == 1