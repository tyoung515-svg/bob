-- Scoped recipe/preparation and publication records. No provider credentials.
CREATE TABLE IF NOT EXISTS work_recipes (
 lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
 revision BIGINT NOT NULL, definition JSONB NOT NULL,
 PRIMARY KEY(lane,owner,id,revision)
);
CREATE TABLE IF NOT EXISTS work_preparations (
 lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
 boundary TEXT NOT NULL, request JSONB NOT NULL, job_request JSONB NOT NULL,
 grant_proposal JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS work_publications (
 lane TEXT NOT NULL, owner TEXT NOT NULL, job_id TEXT NOT NULL,
 request_key TEXT NOT NULL, proposal_digest TEXT NOT NULL, operation_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('intent','completed','conflict','unknown')),
 receipt JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,job_id), UNIQUE(lane,owner,request_key),
 FOREIGN KEY(lane,owner,job_id) REFERENCES tool_jobs(lane,owner,id)
);
CREATE TABLE IF NOT EXISTS chat_questions (
 id TEXT PRIMARY KEY, conversation_id UUID NOT NULL REFERENCES conversations(id),
 user_id TEXT NOT NULL, question TEXT NOT NULL, choices JSONB NOT NULL,
 expires_at DOUBLE PRECISION NOT NULL, answer TEXT, answered_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE work_publications ADD COLUMN IF NOT EXISTS authority JSONB;
CREATE TABLE IF NOT EXISTS chat_work_receipts (
 id BIGSERIAL PRIMARY KEY, conversation_id UUID NOT NULL REFERENCES conversations(id),
 user_id TEXT NOT NULL, tool TEXT NOT NULL, payload JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
DO $$
DECLARE t TEXT;
BEGIN
 FOR t IN SELECT unnest(ARRAY['work_recipes','work_preparations','chat_work_receipts']) LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=t::regclass AND tgname=t||'_immutable') THEN
   EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION tool_job_immutable()',t||'_immutable',t);
  END IF;
 END LOOP;
END $$;
