-- Run-bound Builder sessions use separate conversations and absolute grant life.
ALTER TABLE builder_modes ADD COLUMN IF NOT EXISTS workflow_run_id TEXT;
ALTER TABLE builder_modes ADD COLUMN IF NOT EXISTS workflow_generation BIGINT;
CREATE INDEX IF NOT EXISTS builder_workflow_run ON builder_modes(lane,owner,workflow_run_id)
 WHERE workflow_run_id IS NOT NULL;
