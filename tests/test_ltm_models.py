import sys
import os
import pytest
from datetime import datetime
from pydantic import ValidationError

# Добавляем корень проекта в sys.path
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from models.ltm_models import (
    Message,
    ConversationFragment,
    EmotionalSnapshot,
    LTMEntry,
    MemoryType,
    TriggerReason,
    create_ltm_entry
)


def test_message_creation():
    """Тест создания сообщения с обязательными полями"""
    msg = Message(
        role="user", 
        content="Hello",
        timestamp=datetime.now(),
        message_id="msg_001"
    )
    assert msg.role == "user"
    assert msg.content == "Hello"
    assert msg.message_id == "msg_001"
    assert isinstance(msg.timestamp, datetime)


def test_emotional_snapshot_creation():
    """Тест создания эмоционального снимка через from_dict"""
    emotions_dict = {
        "joy": 0.7, 
        "surprise": 0.5, 
        "neutral": 0.2,
        "unknown_emotion": 0.9  # Должна игнорироваться
    }
    snapshot = EmotionalSnapshot.from_dict(emotions_dict)
    
    assert isinstance(snapshot, EmotionalSnapshot)
    assert snapshot.joy == 0.7
    assert snapshot.surprise == 0.5
    assert snapshot.neutral == 0.2
    
    # Проверяем доминирующие эмоции
    dominant = snapshot.get_dominant_emotions()
    assert "joy" in dominant
    assert "surprise" in dominant
    
    # Проверяем интенсивность
    intensity = snapshot.calculate_intensity()
    assert 0 <= intensity <= 1


def test_ltm_entry_creation():
    """Тест создания полной записи LTM через helper функцию"""
    messages = [
        {
            "role": "user",
            "content": "Hello world",
            "timestamp": datetime.now(),
            "message_id": "msg_001"
        }
    ]
    
    emotions = {
        "joy": 0.5, 
        "surprise": 0.3, 
        "neutral": 0.2
    }
    
    entry = create_ltm_entry(
        user_id="123",
        messages=messages,
        emotions=emotions,
        importance_score=0.75,
        memory_type="self_related",
        trigger_reason="self_reference",
        semantic_tags=["test", "hello"]
    )
    
    assert isinstance(entry, LTMEntry)
    assert entry.user_id == "123"
    assert entry.memory_type == MemoryType.SELF_RELATED
    assert entry.trigger_reason == TriggerReason.SELF_REFERENCE
    assert entry.importance_score == 0.75
    assert "test" in entry.semantic_tags
    assert "hello" in entry.semantic_tags


def test_message_validation_error():
    """Тест валидации неправильной роли"""
    with pytest.raises(ValidationError) as exc:
        Message(
            role="invalid_role", 
            content="Hello",
            timestamp=datetime.now(),
            message_id="msg_001"
        )
    # Проверяем, что ошибка связана с полем role
    assert "role" in str(exc.value)


def test_ltm_entry_to_dict_keys():
    """Тест преобразования LTMEntry в словарь для БД"""
    messages = [
        {
            "role": "user",
            "content": "Hello world", 
            "timestamp": datetime.now(),
            "message_id": "msg_001"
        }
    ]
    
    emotions = {"joy": 0.4, "neutral": 0.6}
    
    entry = create_ltm_entry(
        user_id="123",
        messages=messages,
        emotions=emotions,
        importance_score=0.5,
        memory_type="self_related",
        trigger_reason="self_reference"
    )
    
    # Преобразуем в словарь для БД
    data = entry.to_db_dict()
    
    # Проверяем наличие всех ключевых полей
    expected_keys = [
        'user_id', 'conversation_fragment', 'importance_score',
        'emotional_snapshot', 'dominant_emotions', 'emotional_intensity',
        'memory_type', 'semantic_tags', 'self_relevance_score',
        'trigger_reason', 'accessed_count', 'last_accessed_at'
    ]
    
    for key in expected_keys:
        assert key in data, f"Missing key: {key}"
    
    # Проверяем, что memory_id и created_at исключены
    assert 'memory_id' not in data
    assert 'created_at' not in data
    
    # Проверяем преобразование enum в строки
    assert data['memory_type'] == 'self_related'
    assert data['trigger_reason'] == 'self_reference'


def test_conversation_fragment_validation():
    """Тест валидации ConversationFragment"""
    msg1 = Message(
        role="user",
        content="Hello",
        timestamp=datetime.now(),
        message_id="msg_001"
    )
    
    msg2 = Message(
        role="bot",
        content="Hi there!",
        timestamp=datetime.now(),
        message_id="msg_002",
        mode="talk",
        confidence=0.9
    )
    
    fragment = ConversationFragment(
        messages=[msg1, msg2],
        trigger_message_id="msg_002",
        context_window=2
    )
    
    assert len(fragment.messages) == 2
    assert fragment.trigger_message_id == "msg_002"
    assert fragment.context_window == 2


def test_emotional_snapshot_all_emotions():
    """Тест создания EmotionalSnapshot со всеми эмоциями"""
    all_emotions = {
        'admiration': 0.1, 'amusement': 0.1, 'anger': 0.1, 'annoyance': 0.1,
        'approval': 0.1, 'caring': 0.1, 'confusion': 0.1, 'curiosity': 0.1,
        'desire': 0.1, 'disappointment': 0.1, 'disapproval': 0.1, 'disgust': 0.1,
        'embarrassment': 0.1, 'excitement': 0.1, 'fear': 0.1, 'gratitude': 0.1,
        'grief': 0.1, 'joy': 0.8, 'love': 0.1, 'nervousness': 0.1,
        'optimism': 0.1, 'pride': 0.1, 'realization': 0.1, 'relief': 0.1,
        'remorse': 0.1, 'sadness': 0.1, 'surprise': 0.1, 'neutral': 0.1
    }
    
    snapshot = EmotionalSnapshot.from_dict(all_emotions)
    assert snapshot.joy == 0.8
    
    # Проверяем, что joy - доминирующая эмоция
    dominant = snapshot.get_dominant_emotions(top_n=1)
    assert dominant[0] == "joy"


def test_memory_type_enum():
    """Тест работы с enum MemoryType"""
    assert MemoryType.SELF_RELATED.value == 'self_related'
    assert MemoryType.WORLD_MODEL.value == 'world_model'
    assert MemoryType.USER_RELATED.value == 'user_related'


def test_trigger_reason_enum():
    """Тест работы с enum TriggerReason"""
    assert TriggerReason.EMOTIONAL_PEAK.value == 'emotional_peak'
    assert TriggerReason.SELF_REFERENCE.value == 'self_reference'
    assert len(TriggerReason) >= 7  # Минимум 7 причин