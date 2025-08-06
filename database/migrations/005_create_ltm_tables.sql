-- ========================================
-- Long-Term Memory (LTM) tables
-- ========================================

-- Основная таблица долговременной памяти
CREATE TABLE IF NOT EXISTS ltm_memories (
    -- Идентификация
    memory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    
    -- Контент
    conversation_fragment JSONB NOT NULL,
    importance_score FLOAT NOT NULL CHECK (importance_score >= 0 AND importance_score <= 1),
    
    -- Эмоциональный слой
    emotional_snapshot JSONB NOT NULL,
    dominant_emotions TEXT[] NOT NULL,
    emotional_intensity FLOAT NOT NULL CHECK (emotional_intensity >= 0 AND emotional_intensity <= 1),
    
    -- Семантическая категоризация
    memory_type VARCHAR(50) NOT NULL CHECK (memory_type IN ('self_related', 'world_model', 'user_related')),
    semantic_tags TEXT[] NOT NULL,
    self_relevance_score FLOAT CHECK (self_relevance_score IS NULL OR (self_relevance_score >= 0 AND self_relevance_score <= 1)),
    
    -- Векторное поле будет добавлено в подэтапе 6.1.3
    -- embedding vector(768),
    
    -- Метаданные
    trigger_reason VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    accessed_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP WITH TIME ZONE,
    
    -- Проверка консистентности accessed полей
    CONSTRAINT ltm_access_consistency CHECK (
        (accessed_count = 0 AND last_accessed_at IS NULL) OR 
        (accessed_count > 0 AND last_accessed_at IS NOT NULL)
    )
);

-- ========================================
-- Индексы для оптимизации производительности
-- ========================================

-- Индекс по user_id и created_at для хронологических запросов
CREATE INDEX IF NOT EXISTS idx_ltm_user_timestamp 
    ON ltm_memories(user_id, created_at DESC);

-- GIN индекс для поиска по массиву доминирующих эмоций
CREATE INDEX IF NOT EXISTS idx_ltm_dominant_emotions 
    ON ltm_memories USING GIN (dominant_emotions);

-- GIN индекс для поиска по семантическим тегам
CREATE INDEX IF NOT EXISTS idx_ltm_semantic_tags 
    ON ltm_memories USING GIN (semantic_tags);

-- Индекс по типу памяти для быстрой фильтрации
CREATE INDEX IF NOT EXISTS idx_ltm_memory_type 
    ON ltm_memories(memory_type);

-- Составной индекс по важности и времени для выборки топ воспоминаний
CREATE INDEX IF NOT EXISTS idx_ltm_importance_timestamp 
    ON ltm_memories(importance_score DESC, created_at DESC);

-- Индекс для поиска по причине сохранения
CREATE INDEX IF NOT EXISTS idx_ltm_trigger_reason 
    ON ltm_memories(trigger_reason);

-- Индекс для выборки часто используемых воспоминаний
CREATE INDEX IF NOT EXISTS idx_ltm_accessed 
    ON ltm_memories(user_id, accessed_count DESC)
    WHERE accessed_count > 0;

-- ========================================
-- Комментарии для документации
-- ========================================

COMMENT ON TABLE ltm_memories IS 'Долговременная память Химеры - хранит важные воспоминания с эмоциональным и семантическим контекстом';

-- Основные поля
COMMENT ON COLUMN ltm_memories.memory_id IS 'Уникальный идентификатор воспоминания';
COMMENT ON COLUMN ltm_memories.user_id IS 'Telegram ID пользователя';
COMMENT ON COLUMN ltm_memories.conversation_fragment IS 'Фрагмент диалога: массив сообщений с контекстом момента';
COMMENT ON COLUMN ltm_memories.importance_score IS 'Оценка важности воспоминания (0.0-1.0)';

-- Эмоциональный слой
COMMENT ON COLUMN ltm_memories.emotional_snapshot IS 'Полный эмоциональный вектор из 28 эмоций DeBERTa';
COMMENT ON COLUMN ltm_memories.dominant_emotions IS 'Массив доминирующих эмоций в момент';
COMMENT ON COLUMN ltm_memories.emotional_intensity IS 'Общая интенсивность эмоций (0.0-1.0)';

-- Семантическая категоризация
COMMENT ON COLUMN ltm_memories.memory_type IS 'Тип воспоминания: self_related (о Химере), world_model (о мире), user_related (о пользователе)';
COMMENT ON COLUMN ltm_memories.semantic_tags IS 'Семантические теги для категоризации содержания';
COMMENT ON COLUMN ltm_memories.self_relevance_score IS 'Степень релевантности для самоидентификации Химеры (0.0-1.0, опционально)';

-- Метаданные
COMMENT ON COLUMN ltm_memories.trigger_reason IS 'Причина сохранения: emotional_peak, emotional_shift, self_reference и т.д.';
COMMENT ON COLUMN ltm_memories.accessed_count IS 'Количество обращений к воспоминанию';
COMMENT ON COLUMN ltm_memories.last_accessed_at IS 'Время последнего обращения к воспоминанию';

-- ========================================
-- Функции для работы с LTM
-- ========================================

-- Функция для атомарного обновления счетчика доступа
CREATE OR REPLACE FUNCTION update_ltm_access(
    p_memory_id UUID
) RETURNS VOID AS $$
BEGIN
    UPDATE ltm_memories
    SET 
        accessed_count = accessed_count + 1,
        last_accessed_at = CURRENT_TIMESTAMP
    WHERE memory_id = p_memory_id;
END;
$$ LANGUAGE plpgsql;

-- Функция для получения статистики по эмоциональным паттернам пользователя
CREATE OR REPLACE FUNCTION get_user_emotional_stats(
    p_user_id VARCHAR(255),
    p_days_back INTEGER DEFAULT 30
) RETURNS TABLE(
    emotion TEXT,
    avg_intensity FLOAT,
    occurrence_count BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        unnest(dominant_emotions) as emotion,
        AVG(emotional_intensity) as avg_intensity,
        COUNT(*) as occurrence_count
    FROM ltm_memories
    WHERE 
        user_id = p_user_id 
        AND created_at >= CURRENT_TIMESTAMP - INTERVAL '1 day' * p_days_back
    GROUP BY emotion
    ORDER BY occurrence_count DESC, avg_intensity DESC;
END;
$$ LANGUAGE plpgsql;

-- Grant permissions if needed
-- GRANT ALL ON ltm_memories TO chimera_user;
-- GRANT EXECUTE ON FUNCTION update_ltm_access(UUID) TO chimera_user;
-- GRANT EXECUTE ON FUNCTION get_user_emotional_stats(VARCHAR, INTEGER) TO chimera_user;