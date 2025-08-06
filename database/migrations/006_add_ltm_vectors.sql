-- ========================================
-- Добавление векторной поддержки в LTM
-- ========================================

-- Создание расширения pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- Добавление векторной колонки
ALTER TABLE ltm_memories 
ADD COLUMN IF NOT EXISTS embedding vector(768);

-- Создание индекса для векторного поиска (cosine distance)
CREATE INDEX IF NOT EXISTS idx_ltm_embedding_cosine 
ON ltm_memories 
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

-- Комментарий
COMMENT ON COLUMN ltm_memories.embedding IS 'Композитный embedding: 384d semantic + 128d emotional + 64d temporal + 192d personal';