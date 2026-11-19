-- 4.3d: typed specs remain in agent_run_steps; all execution uses the shared queue.
ALTER TABLE tool_jobs DROP CONSTRAINT IF EXISTS tool_jobs_role_check;
ALTER TABLE tool_jobs ADD CONSTRAINT tool_jobs_role_check CHECK(role IN ('researcher','reviewer','proposer','coder'));
CREATE TABLE IF NOT EXISTS checked_run_contracts (
 lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
 definition JSONB NOT NULL, PRIMARY KEY(lane,owner,run_id),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
 FOREIGN KEY(lane,owner,job_id) REFERENCES tool_jobs(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS checked_snapshots (
 lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, digest TEXT NOT NULL,
 manifest JSONB NOT NULL, PRIMARY KEY(lane,owner,run_id,digest),
 FOREIGN KEY(lane,owner,run_id) REFERENCES checked_run_contracts(lane,owner,run_id)
);
CREATE TABLE IF NOT EXISTS checked_step_jobs (
 lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, ordinal BIGINT NOT NULL,
 execution_id TEXT NOT NULL, workspace_id TEXT NOT NULL, input_digest TEXT NOT NULL,
 spec_digest TEXT NOT NULL, adapter_input_digest TEXT NOT NULL, PRIMARY KEY(lane,owner,run_id,ordinal),
 UNIQUE(lane,owner,execution_id),
 FOREIGN KEY(lane,owner,run_id,ordinal) REFERENCES agent_run_steps(lane,owner,run_id,ordinal),
 CONSTRAINT checked_step_jobs_attempt FOREIGN KEY(lane,owner,execution_id) REFERENCES worker_job_attempts(lane,owner,execution_id) DEFERRABLE INITIALLY IMMEDIATE,
 FOREIGN KEY(lane,owner,run_id,input_digest) REFERENCES checked_snapshots(lane,owner,run_id,digest)
);
CREATE TABLE IF NOT EXISTS checked_step_receipts (
 lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, ordinal BIGINT NOT NULL,
 payload JSONB NOT NULL, PRIMARY KEY(lane,owner,run_id,ordinal),
 FOREIGN KEY(lane,owner,run_id,ordinal) REFERENCES agent_run_steps(lane,owner,run_id,ordinal)
);
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['checked_run_contracts','checked_snapshots','checked_step_jobs','checked_step_receipts'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname=t||'_immutable' AND tgrelid=to_regclass(t)) THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION tool_job_immutable()',t||'_immutable',t);
  END IF;
 END LOOP;
END $$;
