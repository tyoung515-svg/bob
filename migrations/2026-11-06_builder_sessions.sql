-- Builder authority is lane + owner + conversation + immutable project binding.
CREATE TABLE IF NOT EXISTS builder_modes (
 lane TEXT NOT NULL, owner TEXT NOT NULL, conversation_id UUID NOT NULL,
 project_id UUID NOT NULL, mode TEXT NOT NULL DEFAULT 'approve'
 CHECK (mode IN ('suggest','approve','auto','yolo')),
 revision BIGINT NOT NULL DEFAULT 1, expires_at DOUBLE PRECISION,
 session_id TEXT NOT NULL, session_until DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(lane,owner,conversation_id)
);
CREATE TABLE IF NOT EXISTS builder_approvals (
 id TEXT PRIMARY KEY, lane TEXT NOT NULL, owner TEXT NOT NULL,
 conversation_id UUID NOT NULL, project_id UUID NOT NULL, session_id TEXT NOT NULL,
 turn_id TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL,
 summary TEXT NOT NULL, detail JSONB NOT NULL, payload JSONB NOT NULL,
 expires_at DOUBLE PRECISION NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','allowed','denied','expired')),
 execution_state TEXT NOT NULL DEFAULT 'pending'
 CHECK(execution_state IN ('pending','running','completed','unknown')),
 result JSONB, decision_key TEXT, decision_hash TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS builder_approval_conversation ON builder_approvals(lane,owner,conversation_id,created_at);
CREATE TABLE IF NOT EXISTS builder_allowances (
 lane TEXT NOT NULL, owner TEXT NOT NULL, conversation_id UUID NOT NULL,
 session_id TEXT NOT NULL, allowance TEXT NOT NULL,
 PRIMARY KEY(lane,owner,conversation_id,session_id,allowance)
);
CREATE TABLE IF NOT EXISTS builder_checkpoints (
 id TEXT PRIMARY KEY, lane TEXT NOT NULL, owner TEXT NOT NULL,
 conversation_id UUID NOT NULL, project_id UUID NOT NULL, turn_id TEXT NOT NULL,
 snapshot JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(lane,owner,project_id,turn_id)
);
