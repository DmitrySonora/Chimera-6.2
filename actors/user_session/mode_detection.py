from typing import Tuple
from datetime import datetime
import asyncio
import logging
from actors.events import BaseEvent
from config.prompts import MODE_DETECTION_CONFIG
from config.settings import (
    MODE_CONFIDENCE_THRESHOLD,
    MODE_SCORE_NORMALIZATION_FACTOR,
    CONTEXTUAL_PATTERN_PHRASE_WEIGHT,
    CONTEXTUAL_PATTERN_DOMAIN_WEIGHT,
    CONTEXTUAL_PATTERN_CONTEXT_MULTIPLIER,
    CONTEXTUAL_PATTERN_SUPPRESSOR_MULTIPLIER,
    MODE_DETECTION_DEBUG_LOGGING
)

class ModeDetectionMixin:
    def _determine_generation_mode(
        self, 
        text: str, 
        session
    ) -> Tuple[str, float]:
        """
        Определяет режим генерации на основе текста сообщения.
        
        Args:
            text: Текст сообщения пользователя
            session: Текущая сессия пользователя
            
        Returns:
            (режим, уверенность) - режим из GENERATION_MODES и уверенность 0-1
        """
        
        # Базовая проверка
        if not text or len(text) < MODE_DETECTION_CONFIG["min_text_length"]:
            return session.current_mode or 'talk', 0.5
        
        text_lower = text.lower()
        
        # Подсчет очков для каждого режима
        scores = {
            'expert': 0,
            'creative': 0,
            'talk': 0
        }
        
        # Детали определения для логирования
        detection_details = {
            'expert': {'patterns': [], 'score': 0},
            'creative': {'patterns': [], 'score': 0},
            'talk': {'patterns': [], 'score': 0}
        }
        
        # Получаем веса
        weights = MODE_DETECTION_CONFIG["mode_weights"]
        
        # НОВАЯ ЛОГИКА: Проверка контекстных паттернов
        contextual_patterns = MODE_DETECTION_CONFIG.get("contextual_patterns", {})
        
        if contextual_patterns:
            for mode in ['expert', 'creative', 'talk']:
                if mode not in contextual_patterns:
                    continue
                    
                mode_patterns = contextual_patterns[mode]
                
                # Уровень 1: Точные фразы
                for phrase in mode_patterns.get("exact_phrases", []):
                    if phrase in text_lower:
                        phrase_score = weights[mode] * CONTEXTUAL_PATTERN_PHRASE_WEIGHT
                        scores[mode] += phrase_score
                        detection_details[mode]['patterns'].append(f"exact_phrase: {phrase}")
                        
                # Уровень 2: Контекстные слова
                for word, modifiers in mode_patterns.get("contextual_words", {}).items():
                    if word in text_lower:
                        # Базовый вес слова
                        word_score = weights[mode]
                        
                        # Проверяем усилители
                        for enhancer in modifiers.get("enhancers", []):
                            if enhancer in text_lower:
                                word_score *= CONTEXTUAL_PATTERN_CONTEXT_MULTIPLIER
                                detection_details[mode]['patterns'].append(f"enhanced: {word}+{enhancer}")
                                break
                        
                        # Проверяем подавители
                        suppressed = False
                        for suppressor in modifiers.get("suppressors", []):
                            if suppressor in text_lower:
                                word_score *= CONTEXTUAL_PATTERN_SUPPRESSOR_MULTIPLIER
                                suppressed = True
                                detection_details[mode]['patterns'].append(f"suppressed: {word}-{suppressor}")
                                break
                                
                        # Прерываем добавление очков, если suppressor полностью обнуляет
                        if suppressed and CONTEXTUAL_PATTERN_SUPPRESSOR_MULTIPLIER == 0:
                            continue  # подавитель отключил режим полностью
                        
                        # Добавляем очки всегда, подавители уже учтены в word_score
                        scores[mode] += word_score
                            
                # Уровень 3: Доменные маркеры
                domain_count = 0
                for marker in mode_patterns.get("domain_markers", []):
                    if marker in text_lower:
                        domain_count += 1
                        
                if domain_count > 0:
                    # Логарифмическая шкала для доменных маркеров, чтобы много маркеров не давали слишком высокий score
                    import math
                    domain_score = weights[mode] * CONTEXTUAL_PATTERN_DOMAIN_WEIGHT * (1 + math.log(domain_count))
                    scores[mode] += domain_score
                    detection_details[mode]['patterns'].append(f"domains: {domain_count}")
        
        # СТАРАЯ ЛОГИКА: Простые паттерны (fallback)
        if all(score == 0 for score in scores.values()):
            if MODE_DETECTION_DEBUG_LOGGING:
                self.logger.debug("[fallback] all scores are zero, applying simple pattern fallback")
        
            # Получаем паттерны из конфига
            expert_patterns = MODE_DETECTION_CONFIG["expert_patterns"]
            creative_patterns = MODE_DETECTION_CONFIG["creative_patterns"]
            talk_patterns = MODE_DETECTION_CONFIG["talk_patterns"]
            
            # Подсчет совпадений с учетом весов
            for pattern in expert_patterns:
                if pattern in text_lower:
                    scores['expert'] += weights['expert']
                    detection_details['expert']['patterns'].append(f"simple: {pattern}")
                    
            for pattern in creative_patterns:
                if pattern in text_lower:
                    scores['creative'] += weights['creative']
                    detection_details['creative']['patterns'].append(f"simple: {pattern}")
                    
            for pattern in talk_patterns:
                if pattern in text_lower:
                    scores['talk'] += weights['talk']
                    detection_details['talk']['patterns'].append(f"simple: {pattern}")
        
        # Вопросительные слова усиливают expert
        question_words = MODE_DETECTION_CONFIG["question_words"]
        question_bonus = MODE_DETECTION_CONFIG["question_bonus"]
        
        if any(q in text_lower for q in question_words):
            scores['expert'] += question_bonus
            detection_details['expert']['patterns'].append("question_bonus")
        
        # Сохраняем финальные очки
        for mode in scores:
            detection_details[mode]['score'] = scores[mode]
        
        # Определение режима с максимальным счетом
        max_score = max(scores.values())
        if max_score == 0:
            detected_mode = 'talk'
            confidence = MODE_CONFIDENCE_THRESHOLD
        else:
            detected_mode = max(scores, key=scores.get)
            confidence = min(max_score / MODE_SCORE_NORMALIZATION_FACTOR, 1.0)
        
        # Учет истории (если последние 3 сообщения в одном режиме)
        if len(session.mode_history) >= 3:
            last_modes = session.mode_history[-3:]
            if all(m == last_modes[0] for m in last_modes):
                if detected_mode == last_modes[0]:
                    multiplier = MODE_DETECTION_CONFIG["stable_history_multiplier"]
                    confidence = min(confidence * multiplier, 1.0)
                    detection_details[detected_mode]['patterns'].append("history_boost")
        
        # Логирование деталей если включено
        if MODE_DETECTION_DEBUG_LOGGING and self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"Mode detection details for '{text[:50]}...': "
                f"winner={detected_mode} ({confidence:.2f}), "
                f"scores={scores}, "
                f"details={detection_details}"
            )
            
            # Создаем событие для отладки
            if hasattr(self, '_event_version_manager'):
                debug_event = BaseEvent.create(
                    stream_id=f"debug_mode_{session.user_id}",
                    event_type="PatternDebugEvent",
                    data={
                        "user_id": session.user_id,
                        "text_preview": text[:100],
                        "detected_mode": detected_mode,
                        "confidence": confidence,
                        "scores": scores,
                        "detection_details": detection_details,
                        "timestamp": datetime.now().isoformat()
                    }
                )
                # Используем create_task чтобы не блокировать
                asyncio.create_task(self._append_event(debug_event))
        
        # Сохраняем детали для использования в событиях
        self._last_detection_details = detection_details
        
        return detected_mode, confidence