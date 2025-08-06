"""
LTMCoordinationMixin - координация сохранения в долговременную память
"""
from typing import Dict, Any
from datetime import datetime
import uuid
from config.settings import LTM_EMOTIONAL_THRESHOLD, LTM_EMOTIONAL_PEAK_THRESHOLD
from actors.messages import ActorMessage, MESSAGE_TYPES
from models.ltm_models import MemoryType, TriggerReason


class LTMCoordinationMixin:
    """Миксин для координации сохранения в LTM"""
    
    def _should_save_to_ltm(self, emotions: Dict[str, float]) -> bool:
        """
        Простая проверка: максимальная эмоция > порога
        Исключаем neutral из проверки
        
        Args:
            emotions: Словарь эмоций {emotion_name: score}
            
        Returns:
            True если нужно сохранить в LTM
        """
        if not emotions:
            return False
        
        # Фильтруем технические эмоции
        meaningful_emotions = {k: v for k, v in emotions.items() if k != 'neutral'}
        
        if not meaningful_emotions:
            return False
            
        max_emotion_value = max(meaningful_emotions.values())
        return max_emotion_value > LTM_EMOTIONAL_THRESHOLD
    
    def _prepare_ltm_evaluation(
        self, 
        session: Any,
        user_text: str,
        bot_response: str,
        emotions_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Подготовить данные для оценки LTM
        
        Args:
            session: Сессия пользователя
            user_text: Полный текст пользователя
            bot_response: Ответ бота
            emotions_data: Данные от PerceptionActor
            
        Returns:
            Payload для EVALUATE_FOR_LTM сообщения
        """
        # Извлекаем эмоциональные данные
        emotions = emotions_data.get('emotions', {})
        dominant_emotions = emotions_data.get('dominant_emotions', [])
        max_emotion_value = max(emotions.values()) if emotions else 0.0
        
        # Определяем тип памяти
        memory_type = MemoryType.SELF_RELATED if 'химера' in user_text.lower() else MemoryType.USER_RELATED
        
        # Определяем причину сохранения
        trigger_reason = TriggerReason.EMOTIONAL_PEAK if max_emotion_value > LTM_EMOTIONAL_PEAK_THRESHOLD else TriggerReason.EMOTIONAL_SHIFT
        
        # Создаем сообщения для conversation_fragment
        user_message = {
            'role': 'user',
            'content': user_text,
            'timestamp': datetime.now(),
            'message_id': str(uuid.uuid4())
        }
        
        bot_message = {
            'role': 'bot',
            'content': bot_response,
            'timestamp': datetime.now(),
            'message_id': str(uuid.uuid4()),
            'mode': session.last_bot_mode,
            'confidence': session.last_bot_confidence
        }
        
        return {
            'user_id': session.user_id,
            'user_text': user_text,
            'bot_response': bot_response,
            'emotions': emotions,
            'dominant_emotions': dominant_emotions,
            'max_emotion_value': max_emotion_value,
            'mode': session.current_mode,
            'timestamp': datetime.now().isoformat(),
            'memory_type': memory_type.value,
            'trigger_reason': trigger_reason.value,
            'messages': [user_message, bot_message],
            'username': session.username
        }
    
    async def _request_ltm_evaluation(self, payload: Dict[str, Any]) -> None:
        """
        Отправить запрос на оценку в LTMActor
        
        Args:
            payload: Данные для оценки
        """
        if not self.get_actor_system():
            self.logger.warning("No actor system available for LTM evaluation")
            return
            
        evaluate_msg = ActorMessage.create(
            sender_id=self.actor_id,
            message_type=MESSAGE_TYPES['EVALUATE_FOR_LTM'],
            payload=payload
        )
        
        # Fire-and-forget - не ждем ответа
        await self.get_actor_system().send_message("ltm", evaluate_msg)
        
        self.logger.info(
            f"Sent EVALUATE_FOR_LTM for user {payload['user_id']} "
            f"(emotion: {payload['max_emotion_value']:.2f})"
        )