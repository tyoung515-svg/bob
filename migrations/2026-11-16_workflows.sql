-- Workflow definitions and state extend the authoritative execution ledger.
CREATE TABLE IF NOT EXISTS workflow_heads (
 lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL, revision BIGINT NOT NULL,
 deleted BOOLEAN NOT NULL DEFAULT FALSE, boundary TEXT NOT NULL,
 PRIMARY KEY(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS workflow_versions (
 lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL, revision BIGINT NOT NULL,
 definition JSONB NOT NULL, digest TEXT NOT NULL, origin TEXT NOT NULL,
 read_only BOOLEAN NOT NULL, boundary TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,id,revision),
 FOREIGN KEY(lane,owner,id) REFERENCES workflow_heads(lane,owner,id)
);
ALTER TABLE agent_admission_intents ADD COLUMN IF NOT EXISTS workflow_coverage JSONB;
ALTER TABLE agent_admission_intents ADD COLUMN IF NOT EXISTS coverage_digest TEXT;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS workflow_coverage JSONB;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS coverage_digest TEXT;
ALTER TABLE agent_execution_grants DROP CONSTRAINT IF EXISTS agent_execution_grants_matcher_version_check;
ALTER TABLE agent_execution_grants ADD CONSTRAINT agent_execution_grants_matcher_version_check CHECK(matcher_version IN (1,2,3,4));
CREATE UNIQUE INDEX IF NOT EXISTS workflow_proposal_single_grant ON agent_execution_grants(lane,owner,proposal_id)
 WHERE matcher_version=4;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS workflow_revision BIGINT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS topology TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS workflow_state JSONB;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS active_elapsed_ms BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS active_since TIMESTAMPTZ;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS grant_expires_at TIMESTAMPTZ;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS terminal_outcome TEXT;
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS workflow_definition_fk;
ALTER TABLE agent_runs ADD CONSTRAINT workflow_definition_fk FOREIGN KEY(lane,owner,workflow_id,workflow_revision)
 REFERENCES workflow_versions(lane,owner,id,revision);
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check CHECK(status IN
 ('accepted','queued','running','recovering','paused','cancelling','completed','cancelled','failed',
  'pausing','needs_you','stopping','stopped','expired','blocked','unknown'));
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS conversation_id UUID;
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS request_id UUID;
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS checkpoint JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_stage_key ON agent_run_steps(lane,owner,run_id,(spec->>'stage_key'))
 WHERE spec->>'stage_key' IS NOT NULL;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS round_index INTEGER;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS tool_call_id TEXT;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS worker_execution_id TEXT;
ALTER TABLE agent_attempts DROP CONSTRAINT IF EXISTS workflow_worker_attempt_fk;
ALTER TABLE agent_attempts ADD CONSTRAINT workflow_worker_attempt_fk FOREIGN KEY(lane,owner,worker_execution_id)
 REFERENCES worker_job_attempts(lane,owner,execution_id);
CREATE TABLE IF NOT EXISTS workflow_waits (
 lane TEXT NOT NULL,owner TEXT NOT NULL,id TEXT NOT NULL,run_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
 kind TEXT NOT NULL,record_id TEXT NOT NULL,checkpoint_digest TEXT NOT NULL,state TEXT NOT NULL,
 decision JSONB,expires_at TIMESTAMPTZ,revision BIGINT NOT NULL DEFAULT 1,view JSONB NOT NULL,
 PRIMARY KEY(lane,owner,id),
 FOREIGN KEY(lane,owner,run_id,ordinal) REFERENCES agent_run_steps(lane,owner,run_id,ordinal)
);
CREATE TABLE IF NOT EXISTS workflow_messages (
 lane TEXT NOT NULL,owner TEXT NOT NULL,id TEXT NOT NULL,run_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
 text TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),consumed_at TIMESTAMPTZ,
 PRIMARY KEY(lane,owner,id),
 FOREIGN KEY(lane,owner,run_id,ordinal) REFERENCES agent_run_steps(lane,owner,run_id,ordinal)
);
CREATE TABLE IF NOT EXISTS workflow_artifacts (
 lane TEXT NOT NULL,owner TEXT NOT NULL,run_id TEXT NOT NULL,id TEXT NOT NULL,
 digest TEXT NOT NULL,media_type TEXT NOT NULL,bytes BYTEA NOT NULL,
 PRIMARY KEY(lane,owner,run_id,id),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS workflow_workspace_leases (
 lane TEXT NOT NULL,owner TEXT NOT NULL,registration_digest TEXT NOT NULL,run_id TEXT NOT NULL,
 stage_key TEXT NOT NULL,generation BIGINT NOT NULL,lease_until TIMESTAMPTZ NOT NULL,
 base_digest TEXT,quarantined BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(lane,owner,registration_digest),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS workflow_report_outbox (
 lane TEXT NOT NULL,owner TEXT NOT NULL,run_id TEXT NOT NULL,report_digest TEXT NOT NULL,
 message_id UUID NOT NULL,conversation_id UUID,state TEXT NOT NULL,
 PRIMARY KEY(lane,owner,run_id),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id)
);
