-- Update table statistics for query planner
-- psql -U chimera -d chimera_dev -f database/maintenance/vacuum_ltm.sql

VACUUM ANALYZE ltm_memories;