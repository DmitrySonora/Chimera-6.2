"""
Validation and utility mixin for LTMActor - provides validation and helper methods
"""
from typing import Any, List
from models.ltm_models import LTMEntry, MemoryType, TriggerReason
from config.settings import (
    EMOTION_LABELS,
    LTM_EMOTIONAL_PEAK_THRESHOLD,
    LTM_SEMANTIC_TAGS_MAX_SIZE
)
from config.prompts import LTM_SEMANTIC_TAG_KEYWORDS


class LTMValidationMixin:
    """Mixin providing validation and utility methods for LTMActor"""
    
    # These attributes are available from LTMActor
    logger: object
    
    def _validate_emotional_snapshot(self, snapshot: Any) -> None:
        """
        Проверка полноты эмоционального снимка.
        
        Args:
            snapshot: Эмоциональный снимок для проверки
            
        Raises:
            ValueError: Если снимок неполный или невалидный
        """
        # Получаем словарь эмоций
        if hasattr(snapshot, 'to_dict'):
            emotions = snapshot.to_dict()
        else:
            emotions = snapshot
        
        # Проверяем наличие всех 28 эмоций
        missing_emotions = set(EMOTION_LABELS) - set(emotions.keys())
        if missing_emotions:
            self.logger.warning(
                f"Emotional snapshot missing emotions: {missing_emotions}"
            )
        
        # Проверяем, что есть хотя бы одна ненулевая эмоция
        if all(v == 0 for v in emotions.values()):
            raise ValueError("Emotional snapshot has all zero values")
        
        # Проверяем наличие эмоциональных пиков для соответствующего триггера
        if hasattr(snapshot, 'get_dominant_emotions'):
            dominant = snapshot.get_dominant_emotions(top_n=1)[0]
            if emotions.get(dominant, 0) > LTM_EMOTIONAL_PEAK_THRESHOLD:
                self.logger.debug(f"Emotional peak detected: {dominant}={emotions[dominant]:.2f}")
    
    def _extract_semantic_tags(self, conversation_fragment: Any) -> List[str]:
        """
        Базовое извлечение семантических тегов из текста.
        
        Args:
            conversation_fragment: Фрагмент диалога
            
        Returns:
            Список семантических тегов
        """
        tags = []
        
        # Извлекаем текст из фрагмента
        if hasattr(conversation_fragment, 'messages'):
            messages = conversation_fragment.messages
        elif isinstance(conversation_fragment, dict) and 'messages' in conversation_fragment:
            messages = conversation_fragment['messages']
        else:
            return tags
        
        # Анализируем сообщения
        all_text = []
        for msg in messages:
            if hasattr(msg, 'content'):
                all_text.append(msg.content.lower())
            elif isinstance(msg, dict) and 'content' in msg:
                all_text.append(msg['content'].lower())
        
        combined_text = ' '.join(all_text)
        
        # Базовые теги по ключевым словам
        tag_keywords = LTM_SEMANTIC_TAG_KEYWORDS
        
        for tag, keywords in tag_keywords.items():
            if any(kw in combined_text for kw in keywords):
                tags.append(tag)
        
        # Ограничиваем количество тегов
        return tags[:LTM_SEMANTIC_TAGS_MAX_SIZE]
    
    def _calculate_importance(self, ltm_entry: LTMEntry) -> float:
        """
        Рассчитать важность если не передана.
        
        Args:
            ltm_entry: Запись LTM
            
        Returns:
            Оценка важности от 0 до 1
        """
        # Базовая важность
        importance = 0.5
        
        # Увеличиваем за эмоциональную интенсивность
        importance += ltm_entry.emotional_intensity * 0.3
        
        # Увеличиваем за определенные типы памяти
        if ltm_entry.memory_type == MemoryType.SELF_RELATED:
            importance += 0.2
        
        # Увеличиваем за определенные триггеры
        if ltm_entry.trigger_reason in [
            TriggerReason.EMOTIONAL_PEAK,
            TriggerReason.SELF_REFERENCE,
            TriggerReason.PERSONAL_REVELATION
        ]:
            importance += 0.1
        
        # Ограничиваем диапазоном
        return min(max(importance, 0.0), 1.0)