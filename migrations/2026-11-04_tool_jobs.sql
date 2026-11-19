-- 4.3c: service-owned immutable bytes; no worker/database publication authority.
CREATE TABLE IF NOT EXISTS tool_jobs (
 lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL CHECK(id ~ '^[0-9a-f]{32}$'),
 run_id TEXT NOT NULL, actor TEXT NOT NULL, boundary TEXT NOT NULL,
 attachment_id TEXT NOT NULL, project_id TEXT NOT NULL, registration JSONB NOT NULL,
 workspace_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('researcher','reviewer','proposer')),
 request JSONB NOT NULL, base_digest TEXT NOT NULL, output_digest TEXT,
 execution_id TEXT, operation_id TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 1,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,id), UNIQUE(lane,owner,run_id),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
 FOREIGN KEY(lane,owner,execution_id) REFERENCES worker_job_attempts(lane,owner,execution_id)
);
CREATE OR REPLACE FUNCTION tool_job_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.lane,NEW.owner,NEW.id,NEW.run_id,NEW.actor,NEW.boundary,NEW.attachment_id,
        NEW.project_id,NEW.registration,NEW.workspace_id,NEW.role,NEW.request,NEW.base_digest,NEW.operation_id)
 IS DISTINCT FROM
    ROW(OLD.lane,OLD.owner,OLD.id,OLD.run_id,OLD.actor,OLD.boundary,OLD.attachment_id,
        OLD.project_id,OLD.registration,OLD.workspace_id,OLD.role,OLD.request,OLD.base_digest,OLD.operation_id)
 OR (OLD.output_digest IS NOT NULL AND NEW.output_digest IS DISTINCT FROM OLD.output_digest)
 OR (OLD.execution_id IS NOT NULL AND NEW.execution_id IS DISTINCT FROM OLD.execution_id)
 THEN RAISE EXCEPTION 'immutable tool job identity'; END IF;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='tool_jobs_identity_guard' AND tgrelid='tool_jobs'::regclass) THEN
  CREATE TRIGGER tool_jobs_identity_guard BEFORE UPDATE ON tool_jobs FOR EACH ROW EXECUTE FUNCTION tool_job_identity_guard();
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS tool_job_blobs (
 lane TEXT NOT NULL, owner TEXT NOT NULL, sha256 TEXT NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 content BYTEA NOT NULL CHECK(octet_length(content)<=4194304),
 PRIMARY KEY(lane,owner,sha256)
);
CREATE TABLE IF NOT EXISTS tool_job_snapshots (
 lane TEXT NOT NULL, owner TEXT NOT NULL, job_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('base','output')), digest TEXT NOT NULL, manifest JSONB NOT NULL,
 PRIMARY KEY(lane,owner,job_id,kind),
 FOREIGN KEY(lane,owner,job_id) REFERENCES tool_jobs(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS tool_job_artifacts (
 lane TEXT NOT NULL, owner TEXT NOT NULL, job_id TEXT NOT NULL, filename TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 PRIMARY KEY(lane,owner,job_id,filename),
 FOREIGN KEY(lane,owner,job_id) REFERENCES tool_jobs(lane,owner,id),
 FOREIGN KEY(lane,owner,sha256) REFERENCES tool_job_blobs(lane,owner,sha256)
);
CREATE TABLE IF NOT EXISTS job_proposals (
 lane TEXT NOT NULL, owner TEXT NOT NULL, job_id TEXT NOT NULL, digest TEXT NOT NULL,
 payload JSONB NOT NULL, PRIMARY KEY(lane,owner,job_id), UNIQUE(lane,owner,job_id,digest),
 FOREIGN KEY(lane,owner,job_id) REFERENCES tool_jobs(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS job_decisions (
 lane TEXT NOT NULL, owner TEXT NOT NULL, job_id TEXT NOT NULL, proposal_digest TEXT NOT NULL,
 payload JSONB NOT NULL, PRIMARY KEY(lane,owner,job_id),
 FOREIGN KEY(lane,owner,job_id,proposal_digest) REFERENCES job_proposals(lane,owner,job_id,digest)
);
CREATE OR REPLACE FUNCTION tool_job_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable tool job evidence'; END $$;
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['tool_job_blobs','tool_job_snapshots','tool_job_artifacts','job_proposals','job_decisions'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname=t||'_immutable' AND tgrelid=to_regclass(t)) THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION tool_job_immutable()',t||'_immutable',t);
  END IF;
 END LOOP;
END $$;
