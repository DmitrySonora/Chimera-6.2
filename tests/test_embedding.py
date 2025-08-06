import numpy as np
from sentence_transformers import SentenceTransformer

def test_embedding():
    # Загружаем модель
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

    # Генерируем эмбеддинг
    embedding = model.encode("Hello world")

    # Логи
    print("Embedding shape:", embedding.shape)
    print(f"Embedding norm: {np.linalg.norm(embedding):.2f}")

    # Простейшие проверки
    assert isinstance(embedding, np.ndarray)
    assert embedding.shape[0] == 384  # MiniLM-L12-v2 выдаёт 384
    assert np.linalg.norm(embedding) > 0
