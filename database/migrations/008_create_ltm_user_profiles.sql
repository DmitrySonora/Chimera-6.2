-- ========================================
-- LTM User Profiles for novelty assessment
-- ========================================

-- Таблица для хранения эволюционирующего профиля пользователя
CREATE TABLE IF NOT EXISTS ltm_user_profiles (
    user_id TEXT PRIMARY KEY,
    
    -- Калибровочные данные
    total_messages INTEGER DEFAULT 0,
    calibration_complete BOOLEAN DEFAULT FALSE,
    
    -- Статистика для оценки новизны
    emotion_frequencies JSONB DEFAULT '{}',     -- {"joy": 45, "sadness": 12, ...}
    tag_frequencies JSONB DEFAULT '{}',         -- {"philosophy": 5, "identity": 3, ...}
    
    -- Скользящие окна последних оценок
    recent_novelty_scores FLOAT[] DEFAULT '{}', -- Последние 100 оценок
    current_percentile_90 FLOAT DEFAULT 0.8,
    
    -- Эволюция во времени
    last_memory_timestamp TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Индекс для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_ltm_user_profiles_user_id ON ltm_user_profiles(user_id);

-- Триггер для автоматического обновления updated_at
CREATE OR REPLACE FUNCTION update_ltm_user_profiles_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_ltm_user_profiles_timestamp
    BEFORE UPDATE ON ltm_user_profiles
    FOR EACH ROW
    EXECUTE FUNCTION update_ltm_user_profiles_updated_at();

-- Комментарии
COMMENT ON TABLE ltm_user_profiles IS 'Эволюционирующие профили пользователей для оценки новизны воспоминаний';
COMMENT ON COLUMN ltm_user_profiles.user_id IS 'Telegram ID пользователя';
COMMENT ON COLUMN ltm_user_profiles.total_messages IS 'Общее количество обработанных сообщений';
COMMENT ON COLUMN ltm_user_profiles.calibration_complete IS 'Завершена ли калибровка (после буферного периода)';
COMMENT ON COLUMN ltm_user_profiles.emotion_frequencies IS 'Частотность каждой из 28 эмоций';
COMMENT ON COLUMN ltm_user_profiles.tag_frequencies IS 'Частотность семантических тегов';
COMMENT ON COLUMN ltm_user_profiles.recent_novelty_scores IS 'Последние N оценок новизны для расчета перцентиля';
COMMENT ON COLUMN ltm_user_profiles.current_percentile_90 IS '90-й перцентиль текущих оценок новизны';
COMMENT ON COLUMN ltm_user_profiles.last_memory_timestamp IS 'Время последнего сохраненного воспоминания';