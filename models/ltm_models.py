"""
Pydantic модели для Long-Term Memory (LTM)
"""
from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Dict, Any, Optional
from enum import Enum
from datetime import datetime, timezone
from uuid import UUID

from config.settings import (
    LTM_MEMORY_TYPES,
    LTM_TRIGGER_REASONS,
    LTM_SCORE_MIN,
    LTM_SCORE_MAX,
    LTM_MESSAGE_CONTENT_MAX_LENGTH,
    LTM_DOMINANT_EMOTIONS_MAX_SIZE,
    LTM_SEMANTIC_TAGS_MAX_SIZE,
    LTM_CONVERSATION_FRAGMENT_MAX_MESSAGES,
    LTM_CONVERSATION_FRAGMENT_DEFAULT_WINDOW,
    LTM_DEFAULT_ACCESS_COUNT,
    LTM_DEFAULT_SELF_RELEVANCE_SCORE,
    LTM_NOVELTY_SCORES_WINDOW,
    EMOTION_LABELS  # 28 эмоций из DeBERTа
)


class MemoryType(str, Enum):
    """Типы воспоминаний в LTM"""
    SELF_RELATED = 'self_related'      # О самой Химере
    WORLD_MODEL = 'world_model'        # О мире и знаниях
    USER_RELATED = 'user_related'      # О пользователе


class TriggerReason(str, Enum):
    """Причины сохранения в долговременную память"""
    EMOTIONAL_PEAK = 'emotional_peak'                    # Любая эмоция > 0.8
    EMOTIONAL_SHIFT = 'emotional_shift'                  # Резкий перепад состояния
    SELF_REFERENCE = 'self_reference'                    # Упоминание о Химере
    DEEP_INSIGHT = 'deep_insight'                        # Философский/творческий прорыв
    PERSONAL_REVELATION = 'personal_revelation'          # Личная информация пользователя
    RELATIONSHIP_CHANGE = 'relationship_change'          # Изменение в отношениях
    CREATIVE_BREAKTHROUGH = 'creative_breakthrough'      # Особо удачная генерация


class Message(BaseModel):
    """Модель одного сообщения в conversation_fragment"""
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True
    )
    
    role: str = Field(
        ...,
        pattern="^(user|bot)$",
        description="Роль отправителя: user или bot"
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=LTM_MESSAGE_CONTENT_MAX_LENGTH,
        description="Текст сообщения"
    )
    timestamp: datetime = Field(
        ...,
        description="Время отправки сообщения"
    )
    message_id: str = Field(
        ...,
        description="Уникальный идентификатор сообщения"
    )
    
    # Дополнительные поля для сообщений бота
    mode: Optional[str] = Field(
        None,
        pattern="^(talk|expert|creative)$",
        description="Режим генерации (только для bot)"
    )
    confidence: Optional[float] = Field(
        None,
        ge=LTM_SCORE_MIN,
        le=LTM_SCORE_MAX,
        description="Уверенность в ответе (только для bot)"
    )
    
    @field_validator('content')
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        """Проверка, что контент не пустой"""
        if not v or not v.strip():
            raise ValueError('Message content cannot be empty')
        return v.strip()
    
    @field_validator('mode', 'confidence')
    @classmethod
    def bot_only_fields(cls, v: Any, info) -> Any:
        """Проверка, что mode и confidence только для роли bot"""
        if v is not None and info.data.get('role') != 'bot':
            field_name = info.field_name
            raise ValueError(f'{field_name} is only allowed for bot messages')
        return v


class ConversationFragment(BaseModel):
    """Структура фрагмента диалога для сохранения в LTM"""
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True
    )
    
    messages: List[Message] = Field(
        ...,
        min_length=1,
        max_length=LTM_CONVERSATION_FRAGMENT_MAX_MESSAGES,
        description="Массив сообщений контекста"
    )
    trigger_message_id: str = Field(
        ...,
        description="ID сообщения, вызвавшего сохранение"
    )
    context_window: int = Field(
        default=LTM_CONVERSATION_FRAGMENT_DEFAULT_WINDOW,
        ge=1,
        le=LTM_CONVERSATION_FRAGMENT_MAX_MESSAGES,
        description="Размер окна контекста"
    )
    
    @field_validator('messages')
    @classmethod
    def validate_message_order(cls, v: List[Message]) -> List[Message]:
        """Проверка хронологического порядка сообщений"""
        if len(v) > 1:
            for i in range(1, len(v)):
                if v[i].timestamp < v[i-1].timestamp:
                    raise ValueError('Messages must be in chronological order')
        return v
    
    @field_validator('trigger_message_id')
    @classmethod
    def validate_trigger_exists(cls, v: str, info) -> str:
        """Проверка, что trigger_message_id существует в messages"""
        messages = info.data.get('messages', [])
        message_ids = [msg.message_id for msg in messages]
        if v not in message_ids:
            raise ValueError('trigger_message_id must reference an existing message')
        return v


class EmotionalSnapshot(BaseModel):
    """Полный эмоциональный вектор момента (28 эмоций DeBERTa)"""
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        extra='forbid'  # Запрещаем лишние поля
    )
    
    # Создаем поля для всех 28 эмоций динамически
    admiration: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    amusement: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    anger: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    annoyance: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    approval: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    caring: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    confusion: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    curiosity: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    desire: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    disappointment: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    disapproval: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    disgust: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    embarrassment: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    excitement: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    fear: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    gratitude: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    grief: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    joy: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    love: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    nervousness: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    optimism: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    pride: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    realization: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    relief: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    remorse: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    sadness: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    surprise: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    neutral: float = Field(default=0.0, ge=LTM_SCORE_MIN, le=LTM_SCORE_MAX)
    
    @classmethod
    def from_dict(cls, emotions_dict: Dict[str, float]) -> 'EmotionalSnapshot':
        """Создать из словаря эмоций"""
        # Фильтруем только известные эмоции
        known_emotions = {k: v for k, v in emotions_dict.items() if k in EMOTION_LABELS}
        return cls(**known_emotions)
    
    def to_dict(self) -> Dict[str, float]:
        """Преобразовать в словарь для сохранения в JSONB"""
        return self.model_dump()
    
    def get_dominant_emotions(self, top_n: int = 3, threshold: float = 0.1) -> List[str]:
        """Получить доминирующие эмоции"""
        emotions = self.model_dump()
        # Фильтруем эмоции выше порога
        filtered = [(k, v) for k, v in emotions.items() if v > threshold]
        # Сортируем по убыванию значения
        sorted_emotions = sorted(filtered, key=lambda x: x[1], reverse=True)
        # Возвращаем топ N названий
        result = [emotion for emotion, _ in sorted_emotions[:top_n]]
        # Если все эмоции нулевые, возвращаем хотя бы neutral
        if not result:
            result = ['neutral']
        return result
    
    def calculate_intensity(self) -> float:
        """Рассчитать общую эмоциональную интенсивность"""
        emotions = self.model_dump()
        # Исключаем neutral из расчета
        meaningful_emotions = {k: v for k, v in emotions.items() if k != 'neutral'}
        if not meaningful_emotions:
            return 0.0
        # Возвращаем максимальную эмоцию как показатель интенсивности
        return max(meaningful_emotions.values())


class LTMEntry(BaseModel):
    """Основная модель записи в долговременной памяти"""
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True
    )
    
    # Идентификация
    memory_id: Optional[UUID] = Field(
        None,
        description="UUID воспоминания (генерируется БД)"
    )
    user_id: str = Field(
        ...,
        min_length=1,
        description="Telegram ID пользователя"
    )
    
    # Контент
    conversation_fragment: ConversationFragment = Field(
        ...,
        description="Фрагмент диалога с контекстом"
    )
    importance_score: float = Field(
        ...,
        ge=LTM_SCORE_MIN,
        le=LTM_SCORE_MAX,
        description="Оценка важности воспоминания"
    )
    
    # Эмоциональный слой
    emotional_snapshot: EmotionalSnapshot = Field(
        ...,
        description="Полный эмоциональный вектор"
    )
    dominant_emotions: List[str] = Field(
        ...,
        min_length=1,
        max_length=LTM_DOMINANT_EMOTIONS_MAX_SIZE,
        description="Доминирующие эмоции"
    )
    emotional_intensity: float = Field(
        ...,
        ge=LTM_SCORE_MIN,
        le=LTM_SCORE_MAX,
        description="Общая интенсивность эмоций"
    )
    
    # Семантическая категоризация
    memory_type: MemoryType = Field(
        ...,
        description="Тип воспоминания"
    )
    semantic_tags: List[str] = Field(
        default_factory=list,
        max_length=LTM_SEMANTIC_TAGS_MAX_SIZE,
        description="Семантические теги"
    )
    self_relevance_score: Optional[float] = Field(
        LTM_DEFAULT_SELF_RELEVANCE_SCORE,
        ge=LTM_SCORE_MIN,
        le=LTM_SCORE_MAX,
        description="Релевантность для самоидентификации"
    )
    
    # Метаданные
    trigger_reason: TriggerReason = Field(
        ...,
        description="Причина сохранения"
    )
    created_at: Optional[datetime] = Field(
        None,
        description="Время создания (заполняется БД)"
    )
    accessed_count: int = Field(
        default=LTM_DEFAULT_ACCESS_COUNT,
        ge=0,
        description="Количество обращений"
    )
    last_accessed_at: Optional[datetime] = Field(
        None,
        description="Время последнего доступа"
    )
    
    @field_validator('dominant_emotions')
    @classmethod
    def validate_emotions_exist(cls, v: List[str]) -> List[str]:
        """Проверка, что эмоции из списка известных"""
        for emotion in v:
            if emotion not in EMOTION_LABELS:
                raise ValueError(f'Unknown emotion: {emotion}')
        return v
    
    @field_validator('semantic_tags')
    @classmethod
    def clean_semantic_tags(cls, v: List[str]) -> List[str]:
        """Очистка и валидация семантических тегов"""
        # Убираем пустые строки и дубликаты
        cleaned = list(set(tag.strip().lower() for tag in v if tag.strip()))
        return cleaned
    
    @field_validator('memory_type')
    @classmethod
    def validate_memory_type(cls, v: MemoryType) -> MemoryType:
        """Дополнительная проверка типа памяти"""
        if v.value not in LTM_MEMORY_TYPES:
            raise ValueError(f'Invalid memory type: {v.value}')
        return v
    
    @field_validator('trigger_reason')
    @classmethod
    def validate_trigger_reason(cls, v: TriggerReason) -> TriggerReason:
        """Дополнительная проверка причины сохранения"""
        if v.value not in LTM_TRIGGER_REASONS:
            raise ValueError(f'Invalid trigger reason: {v.value}')
        return v
    
    def to_db_dict(self) -> Dict[str, Any]:
        """Преобразовать для сохранения в БД"""
        data = self.model_dump(exclude={'memory_id', 'created_at'})
        
        # Преобразуем conversation_fragment и все datetime в нем
        if isinstance(data['conversation_fragment'], dict):
            for msg in data['conversation_fragment'].get('messages', []):
                if 'timestamp' in msg and hasattr(msg['timestamp'], 'isoformat'):
                    msg['timestamp'] = msg['timestamp'].isoformat()
        
        # Преобразуем emotional_snapshot
        data['emotional_snapshot'] = data['emotional_snapshot'].to_dict() if isinstance(data['emotional_snapshot'], EmotionalSnapshot) else data['emotional_snapshot']
        
        # Преобразуем enum в строки
        data['memory_type'] = data['memory_type'].value if isinstance(data['memory_type'], MemoryType) else data['memory_type']
        data['trigger_reason'] = data['trigger_reason'].value if isinstance(data['trigger_reason'], TriggerReason) else data['trigger_reason']
        
        # Преобразуем last_accessed_at если есть
        if 'last_accessed_at' in data and data['last_accessed_at'] and hasattr(data['last_accessed_at'], 'isoformat'):
            data['last_accessed_at'] = data['last_accessed_at'].isoformat()
            
        return data


# Вспомогательные функции для работы с LTM

def create_ltm_entry(
    user_id: str,
    messages: List[Dict[str, Any]],
    emotions: Dict[str, float],
    importance_score: float,
    memory_type: str,
    trigger_reason: str,
    semantic_tags: Optional[List[str]] = None,
    self_relevance_score: Optional[float] = None
) -> LTMEntry:
    """
    Вспомогательная функция для создания записи LTM
    
    Args:
        user_id: ID пользователя
        messages: Список сообщений для conversation_fragment
        emotions: Словарь эмоций для emotional_snapshot
        importance_score: Оценка важности
        memory_type: Тип памяти
        trigger_reason: Причина сохранения
        semantic_tags: Семантические теги
        self_relevance_score: Релевантность для Химеры
        
    Returns:
        LTMEntry готовый для сохранения
    """
    # Создаем conversation fragment
    trigger_message_id = messages[-1]['message_id'] if messages else 'unknown'
    conversation_fragment = ConversationFragment(
        messages=[Message(**msg) for msg in messages],
        trigger_message_id=trigger_message_id,
        context_window=len(messages)
    )
    
    # Создаем emotional snapshot
    emotional_snapshot = EmotionalSnapshot.from_dict(emotions)
    dominant_emotions = emotional_snapshot.get_dominant_emotions()
    emotional_intensity = emotional_snapshot.calculate_intensity()
    
    # Создаем LTM entry
    return LTMEntry(
        user_id=user_id,
        conversation_fragment=conversation_fragment,
        importance_score=importance_score,
        emotional_snapshot=emotional_snapshot,
        dominant_emotions=dominant_emotions,
        emotional_intensity=emotional_intensity,
        memory_type=MemoryType(memory_type),
        semantic_tags=semantic_tags or [],
        self_relevance_score=self_relevance_score,
        trigger_reason=TriggerReason(trigger_reason)
    )


class LTMUserProfile(BaseModel):
    """Эволюционирующий профиль пользователя для оценки новизны воспоминаний"""
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True
    )
    
    # Идентификация
    user_id: str = Field(
        ...,
        min_length=1,
        description="Telegram ID пользователя"
    )
    
    # Калибровочные данные
    total_messages: int = Field(
        default=0,
        ge=0,
        description="Общее количество обработанных сообщений"
    )
    calibration_complete: bool = Field(
        default=False,
        description="Завершена ли калибровка (после буферного периода)"
    )
    
    # Статистика для оценки новизны
    emotion_frequencies: Dict[str, int] = Field(
        default_factory=dict,
        description="Частотность каждой из 28 эмоций"
    )
    tag_frequencies: Dict[str, int] = Field(
        default_factory=dict,
        description="Частотность семантических тегов"
    )
    
    # Скользящие окна последних оценок
    recent_novelty_scores: List[float] = Field(
        default_factory=list,
        max_length=LTM_NOVELTY_SCORES_WINDOW,
        description="Последние оценки новизны для расчета перцентиля"
    )
    current_percentile_90: float = Field(
        default=0.8,
        ge=LTM_SCORE_MIN,
        le=LTM_SCORE_MAX,
        description="90-й перцентиль текущих оценок новизны"
    )
    
    # Эволюция во времени
    last_memory_timestamp: Optional[datetime] = Field(
        None,
        description="Время последнего сохраненного воспоминания"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Время создания профиля"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Время последнего обновления (обновляется триггером БД)"
    )
    
    @field_validator('recent_novelty_scores')
    @classmethod
    def validate_scores_window(cls, v: List[float]) -> List[float]:
        """Проверка размера окна оценок"""
        if len(v) > LTM_NOVELTY_SCORES_WINDOW:
            # Оставляем только последние N элементов
            return v[-LTM_NOVELTY_SCORES_WINDOW:]
        return v
    
    @field_validator('emotion_frequencies', 'tag_frequencies')
    @classmethod
    def validate_frequencies(cls, v: Dict[str, int]) -> Dict[str, int]:
        """Проверка, что все значения неотрицательные"""
        for key, value in v.items():
            if value < 0:
                raise ValueError(f'Frequency for {key} cannot be negative')
        return v
    
    def to_db_dict(self) -> Dict[str, Any]:
        """Преобразовать для сохранения в БД"""
        data = self.model_dump(exclude={'created_at', 'updated_at'})
        
        # last_memory_timestamp может требовать преобразования
        if data.get('last_memory_timestamp') and hasattr(data['last_memory_timestamp'], 'isoformat'):
            data['last_memory_timestamp'] = data['last_memory_timestamp'].isoformat()
            
        return data