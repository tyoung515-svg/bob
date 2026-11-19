-- S3b/3.1: immutable proposals/versions, selected grants and disabled pool counters.
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS matcher_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS coverage_kind TEXT NOT NULL DEFAULT 'task';
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS proposal_id TEXT;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS credential_epoch BIGINT;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS allow_extended_jobs BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS allow_custom_job_limit BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS job_seconds_ceiling BIGINT CHECK(job_seconds_ceiling>0);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS selected_grant_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS selected_grant_revision BIGINT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS agent_admission_intents (
 lane TEXT NOT NULL,owner TEXT NOT NULL,id TEXT NOT NULL,revision BIGINT NOT NULL DEFAULT 1,
 run_id TEXT NOT NULL,boundary TEXT NOT NULL,authority JSONB NOT NULL,request JSONB NOT NULL,
 snapshot JSONB NOT NULL,proposal_digest TEXT NOT NULL,consumed BOOLEAN NOT NULL DEFAULT FALSE,
 existing_run BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS agent_grant_versions (
 lane TEXT NOT NULL,owner TEXT NOT NULL,id TEXT NOT NULL,revision BIGINT NOT NULL,
 record JSONB NOT NULL,actor TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,id,revision)
);
ALTER TABLE agent_grant_versions ADD COLUMN IF NOT EXISTS issuer_authority JSONB NOT NULL DEFAULT '{}';
ALTER TABLE agent_admission_intents ADD COLUMN IF NOT EXISTS grant_id TEXT;
ALTER TABLE agent_admission_intents ADD COLUMN IF NOT EXISTS grant_revision BIGINT;
ALTER TABLE agent_call_pools ALTER COLUMN call_limit DROP NOT NULL;
ALTER TABLE agent_call_pools ADD COLUMN IF NOT EXISTS counter_coverage TEXT NOT NULL DEFAULT 'durable_runs_only';
CREATE TABLE IF NOT EXISTS agent_execution_migrations(id TEXT PRIMARY KEY);
DO $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM agent_execution_migrations WHERE id='s3b-project-counters') THEN
  -- A one-time deterministic snapshot, including runs predating the first pool.
  -- Other pre-durable/legacy model usage has no accounting source and is excluded.
  LOCK TABLE agent_runs,agent_budget_accounts,agent_call_pools,agent_pool_holds IN SHARE ROW EXCLUSIVE MODE;
  INSERT INTO agent_call_pools(lane,owner,kind,id,call_limit)
   SELECT DISTINCT lane,owner,'project',request->>'project_id',NULL::bigint FROM agent_runs
   WHERE request->>'project_id' IS NOT NULL ON CONFLICT DO NOTHING;
  INSERT INTO agent_pool_holds(lane,owner,run_id,kind,pool_id,remaining)
   SELECT r.lane,r.owner,r.id,'project',r.request->>'project_id',
    CASE WHEN r.status IN ('accepted','queued','running','recovering') THEN a.call_limit-a.consumed ELSE 0 END
   FROM agent_runs r JOIN agent_budget_accounts a ON (r.lane,r.owner,r.id)=(a.lane,a.owner,a.run_id)
   WHERE r.request->>'project_id' IS NOT NULL ON CONFLICT DO NOTHING;
  -- A historically unenforced pool can have more commitment than its old limit.
  -- Fail the migration instead of inventing credit or silently widening a limit.
  UPDATE agent_call_pools p SET consumed=c.used,reserved=c.held FROM (
   SELECT r.lane,r.owner,r.request->>'project_id' AS project_id,SUM(a.consumed)::bigint AS used,
    SUM(h.remaining)::bigint AS held FROM agent_runs r
   JOIN agent_budget_accounts a ON (r.lane,r.owner,r.id)=(a.lane,a.owner,a.run_id)
   JOIN agent_pool_holds h ON (r.lane,r.owner,r.id)=(h.lane,h.owner,h.run_id) AND h.kind='project'
   GROUP BY r.lane,r.owner,r.request->>'project_id'
  ) c WHERE p.kind='project' AND (p.lane,p.owner,p.id)=(c.lane,c.owner,c.project_id);
  INSERT INTO agent_execution_migrations VALUES ('s3b-project-counters');
 END IF;
END $$;
