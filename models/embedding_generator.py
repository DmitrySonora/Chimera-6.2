"""
Генератор композитных embeddings для LTM
"""
import os
os.environ['SENTENCE_TRANSFORMERS_HOME'] = 'data/models'  # или значение из LTM_EMBEDDING_CACHE_DIR
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

import numpy as np
from typing import Dict, List, Any
from datetime import datetime
import math
import warnings
warnings.filterwarnings('ignore', category=FutureWarning, module='torch')

from sentence_transformers import SentenceTransformer  # noqa: E402
import logging  # noqa: E402

from config.settings import (  # noqa: E402
    LTM_EMBEDDING_MODEL, LTM_EMBEDDING_DEVICE, LTM_EMBEDDING_CACHE_DIR,
    LTM_EMBEDDING_SEMANTIC_DIM, LTM_EMBEDDING_EMOTIONAL_DIM,
    LTM_EMBEDDING_TEMPORAL_DIM, LTM_EMBEDDING_PERSONAL_DIM,
    LTM_EMBEDDING_MAX_LENGTH, LTM_EMBEDDING_NORMALIZE,
    EMOTION_LABELS
)


class EmbeddingGenerator:
    """Генератор композитных embeddings для LTM"""
    
    def __init__(self):
        """Инициализация модели"""
        self.logger = logging.getLogger(f"{__name__}.EmbeddingGenerator")
        
        try:
            self.model = SentenceTransformer(
                LTM_EMBEDDING_MODEL,
                device=LTM_EMBEDDING_DEVICE,
                cache_folder=LTM_EMBEDDING_CACHE_DIR
            )
            self.logger.info(f"Loaded: {LTM_EMBEDDING_MODEL}")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize embedding model: {e}")
            raise
        
    def generate_semantic_embedding(self, text: str) -> np.ndarray:
        """
        Генерация семантического embedding из текста
        
        Args:
            text: Текст для анализа
            
        Returns:
            Вектор размерности LTM_EMBEDDING_SEMANTIC_DIM
        """
        if not text or not text.strip():
            return np.zeros(LTM_EMBEDDING_SEMANTIC_DIM)
        
        # Обрезаем текст если слишком длинный
        if len(text) > LTM_EMBEDDING_MAX_LENGTH:
            text = text[:LTM_EMBEDDING_MAX_LENGTH]
        
        # Генерируем embedding
        embedding = self.model.encode(text, convert_to_numpy=True, show_progress_bar=False)
        
        # Приводим к нужной размерности
        if len(embedding) >= LTM_EMBEDDING_SEMANTIC_DIM:
            result = embedding[:LTM_EMBEDDING_SEMANTIC_DIM]
        else:
            # Дополняем нулями если embedding меньше
            result = np.pad(embedding, (0, LTM_EMBEDDING_SEMANTIC_DIM - len(embedding)))
        
        return result.astype(np.float32)
        
    def generate_emotional_embedding(self, emotional_snapshot: Dict[str, float]) -> np.ndarray:
        """
        Генерация эмоционального embedding из snapshot
        
        Args:
            emotional_snapshot: Словарь с 28 эмоциями
            
        Returns:
            Вектор размерности LTM_EMBEDDING_EMOTIONAL_DIM (128)
        """
        # Создаем вектор из эмоций в правильном порядке
        emotion_vector = np.array([
            emotional_snapshot.get(emotion, 0.0) 
            for emotion in EMOTION_LABELS
        ])
        
        # Стратегия: расширяем 28 эмоций до 128 измерений
        # через интерполяцию и производные признаки
        
        result = np.zeros(LTM_EMBEDDING_EMOTIONAL_DIM)
        
        # 1. Первые 28 измерений - сами эмоции
        result[:28] = emotion_vector
        
        # 2. Следующие 28 - квадраты значений (нелинейность)
        result[28:56] = emotion_vector ** 2
        
        # 3. Следующие 28 - попарные произведения соседних эмоций
        for i in range(28):
            j = (i + 1) % 28
            result[56 + i] = emotion_vector[i] * emotion_vector[j]
        
        # 4. Следующие 16 - агрегированные признаки
        if emotion_vector.sum() > 0:
            # Позитивные эмоции
            positive_emotions = ['joy', 'love', 'optimism', 'excitement', 'gratitude', 'pride']
            positive_indices = [EMOTION_LABELS.index(e) for e in positive_emotions if e in EMOTION_LABELS]
            result[84] = np.mean([emotion_vector[i] for i in positive_indices])
            
            # Негативные эмоции
            negative_emotions = ['anger', 'fear', 'sadness', 'disgust', 'disappointment']
            negative_indices = [EMOTION_LABELS.index(e) for e in negative_emotions if e in EMOTION_LABELS]
            result[85] = np.mean([emotion_vector[i] for i in negative_indices])
            
            # Общая интенсивность
            result[86] = np.mean(emotion_vector)
            result[87] = np.max(emotion_vector)
            result[88] = np.std(emotion_vector)
            
            # Доминирование (разброс между эмоциями)
            result[89] = np.max(emotion_vector) - np.min(emotion_vector)
        
        # 5. Оставшиеся измерения заполняем интерполяцией
        # между существующими значениями
        filled_dims = 90
        remaining = LTM_EMBEDDING_EMOTIONAL_DIM - filled_dims
        
        if remaining > 0 and emotion_vector.sum() > 0:
            # Создаем плавный переход через интерполяцию
            x_old = np.linspace(0, 1, filled_dims)
            x_new = np.linspace(0, 1, LTM_EMBEDDING_EMOTIONAL_DIM)
            result = np.interp(x_new, x_old, result[:filled_dims])
        
        return result.astype(np.float32)
        
    def generate_temporal_embedding(self, timestamp: Any) -> np.ndarray:
        """
        Генерация временного embedding
        
        Args:
            timestamp: Временная метка (datetime или строка)
            
        Returns:
            Вектор размерности LTM_EMBEDDING_TEMPORAL_DIM
        """
        # Преобразуем в datetime если нужно
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif not isinstance(timestamp, datetime):
            timestamp = datetime.now()
        
        # Извлекаем временные компоненты
        hour = timestamp.hour
        day_of_week = timestamp.weekday()
        day_of_month = timestamp.day
        month = timestamp.month
        
        # Синусоидальное кодирование для циклических величин
        features = []
        
        # Час дня (24-часовой цикл)
        features.extend([
            math.sin(2 * math.pi * hour / 24),
            math.cos(2 * math.pi * hour / 24)
        ])
        
        # День недели (7-дневный цикл)
        features.extend([
            math.sin(2 * math.pi * day_of_week / 7),
            math.cos(2 * math.pi * day_of_week / 7)
        ])
        
        # День месяца (30-дневный цикл)
        features.extend([
            math.sin(2 * math.pi * day_of_month / 30),
            math.cos(2 * math.pi * day_of_month / 30)
        ])
        
        # Месяц (12-месячный цикл)
        features.extend([
            math.sin(2 * math.pi * month / 12),
            math.cos(2 * math.pi * month / 12)
        ])
        
        # Дополняем до нужной размерности
        temporal_vector = np.array(features)
        if len(temporal_vector) < LTM_EMBEDDING_TEMPORAL_DIM:
            temporal_vector = np.pad(
                temporal_vector, 
                (0, LTM_EMBEDDING_TEMPORAL_DIM - len(temporal_vector))
            )
        else:
            temporal_vector = temporal_vector[:LTM_EMBEDDING_TEMPORAL_DIM]
        
        return temporal_vector.astype(np.float32)
        
    def generate_personal_embedding(self, semantic_tags: List[str], memory_type: str) -> np.ndarray:
        """
        Генерация персонального embedding из тегов и типа
        
        Args:
            semantic_tags: Список семантических тегов
            memory_type: Тип памяти (self_related/world_model/user_related)
            
        Returns:
            Вектор размерности LTM_EMBEDDING_PERSONAL_DIM
        """
        # One-hot encoding для типа памяти (первые 3 измерения)
        memory_type_vector = np.zeros(3)
        memory_type_map = {
            'self_related': 0,
            'world_model': 1,
            'user_related': 2
        }
        if memory_type in memory_type_map:
            memory_type_vector[memory_type_map[memory_type]] = 1.0
        
        # Embeddings для тегов
        if semantic_tags:
            # Объединяем теги в текст
            tags_text = " ".join(semantic_tags)
            tags_embedding = self.model.encode(tags_text, convert_to_numpy=True, show_progress_bar=False)
            
            # Берем первые N измерений
            remaining_dims = LTM_EMBEDDING_PERSONAL_DIM - 3
            if len(tags_embedding) >= remaining_dims:
                tags_vector = tags_embedding[:remaining_dims]
            else:
                tags_vector = np.pad(
                    tags_embedding, 
                    (0, remaining_dims - len(tags_embedding))
                )
        else:
            tags_vector = np.zeros(LTM_EMBEDDING_PERSONAL_DIM - 3)
        
        # Объединяем
        personal_vector = np.concatenate([memory_type_vector, tags_vector])
        
        return personal_vector.astype(np.float32)
        
    def generate_composite_embedding(
        self,
        text: str,
        emotional_snapshot: Dict[str, float],
        timestamp: Any,
        semantic_tags: List[str],
        memory_type: str
    ) -> np.ndarray:
        """
        Генерация композитного 768d embedding
        
        Args:
            text: Текст для семантического анализа
            emotional_snapshot: Словарь эмоций
            timestamp: Временная метка
            semantic_tags: Семантические теги
            memory_type: Тип памяти
            
        Returns:
            Нормализованный вектор размерности 768
        """
        try:
            # Генерируем компоненты
            semantic = self.generate_semantic_embedding(text)
            emotional = self.generate_emotional_embedding(emotional_snapshot)
            temporal = self.generate_temporal_embedding(timestamp)
            personal = self.generate_personal_embedding(semantic_tags, memory_type)
            
            # Объединяем в единый вектор
            composite = np.concatenate([
                semantic,   # 384d
                emotional,  # 128d
                temporal,   # 64d
                personal    # 192d
            ])
            
            # Проверяем размерность
            assert len(composite) == 768, f"Wrong embedding size: {len(composite)}"
            
            # Нормализуем если требуется
            if LTM_EMBEDDING_NORMALIZE:
                norm = np.linalg.norm(composite)
                if norm > 0:
                    composite = composite / norm
            
            return composite.astype(np.float32)
            
        except Exception as e:
            self.logger.error(f"Failed to generate composite embedding: {e}")
            raise