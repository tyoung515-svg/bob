-- Requests and results only. Grants live exclusively in the Windows app.
CREATE TABLE IF NOT EXISTS pc_tasks (
 id TEXT PRIMARY KEY, lane TEXT NOT NULL, owner TEXT NOT NULL, agent TEXT NOT NULL,
 principal TEXT NOT NULL, conversation_id UUID NOT NULL, credential_epoch BIGINT NOT NULL,
 created_at DOUBLE PRECISION NOT NULL, ended_at DOUBLE PRECISION,
 UNIQUE(lane,owner,agent,principal,conversation_id,credential_epoch,id)
);
CREATE UNIQUE INDEX IF NOT EXISTS pc_live_task ON pc_tasks
 (lane,owner,agent,principal,conversation_id,credential_epoch) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS pc_requests (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES pc_tasks(id),
 operation TEXT NOT NULL, args JSONB NOT NULL, args_sha256 TEXT NOT NULL,
 request_key TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN('pending','running','done','failed','denied','expired','stopped','unknown')),
 host_id TEXT, claim_nonce TEXT, receipt JSONB,
 UNIQUE(task_id,request_key)
);
