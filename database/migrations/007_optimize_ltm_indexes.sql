-- Migration 007: Optimize LTM indexes for vector search performance

-- Drop old index if exists
DROP INDEX IF EXISTS idx_ltm_embedding_cosine;

-- Create optimized IVFFlat index for vector search
-- lists = 200 is good for ~40k vectors (sqrt(40000) ≈ 200)
CREATE INDEX IF NOT EXISTS idx_ltm_embedding_ivfflat 
ON ltm_memories 
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 200);

-- Create composite index for frequent queries
CREATE INDEX IF NOT EXISTS idx_ltm_user_importance_created 
ON ltm_memories(user_id, importance_score DESC, created_at DESC);

-- Create index for emotional intensity queries
CREATE INDEX IF NOT EXISTS idx_ltm_user_emotional_intensity
ON ltm_memories(user_id, emotional_intensity DESC);

-- Add comment about index maintenance
COMMENT ON INDEX idx_ltm_embedding_ivfflat IS 
'IVFFlat index for fast vector similarity search. Rebuild when data grows 10x';