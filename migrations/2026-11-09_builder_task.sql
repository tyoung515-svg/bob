-- Task authority survives transport closure, but never a boundary change.
ALTER TABLE builder_modes ADD COLUMN IF NOT EXISTS authority_scope TEXT;
ALTER TABLE builder_modes ADD COLUMN IF NOT EXISTS credential_epoch BIGINT;
ALTER TABLE builder_approvals ADD COLUMN IF NOT EXISTS session_until DOUBLE PRECISION;
ALTER TABLE builder_approvals ADD COLUMN IF NOT EXISTS task_authorized BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE builder_approvals ADD COLUMN IF NOT EXISTS execution_until DOUBLE PRECISION;
ALTER TABLE builder_approvals DROP CONSTRAINT IF EXISTS builder_approvals_state_check;
ALTER TABLE builder_approvals ADD CONSTRAINT builder_approvals_state_check
 CHECK(state IN ('pending','allowed','denied','expired','stopped'));
ALTER TABLE builder_approvals DROP CONSTRAINT IF EXISTS builder_approvals_execution_state_check;
ALTER TABLE builder_approvals ADD CONSTRAINT builder_approvals_execution_state_check
 CHECK(execution_state IN ('pending','running','completed','failed','stopped','unknown'));
-- Historical authorization is retained. A known nonzero exit is a failed execution.
UPDATE builder_approvals SET execution_state='failed'
 WHERE execution_state='completed' AND
 (result->'error' IS NOT NULL OR COALESCE(result->>'exit_code','0') <> '0');
-- Never dispatch expired proposals, including old C5f records, during upgrade.
UPDATE builder_approvals SET state='expired',
 result=COALESCE(result,'{"status":"expired","error":{"code":"approval_expired"}}'::jsonb)
 WHERE state='pending' AND expires_at<=extract(epoch FROM now());
