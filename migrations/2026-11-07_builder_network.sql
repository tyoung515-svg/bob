-- Project edits are owner-only API operations, never workspace/model writes.
CREATE TABLE IF NOT EXISTS builder_network_projects (
    lane TEXT NOT NULL, owner TEXT NOT NULL, project_id UUID NOT NULL,
    domains JSONB NOT NULL DEFAULT '[]', revision BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (lane, owner, project_id)
);
CREATE TABLE IF NOT EXISTS builder_network_log (
    id BIGSERIAL PRIMARY KEY, lane TEXT NOT NULL, owner TEXT NOT NULL,
    conversation_id UUID NOT NULL, project_id UUID NOT NULL,
    job_id TEXT NOT NULL, approval_id TEXT, domain TEXT NOT NULL,
    method TEXT NOT NULL, bytes_in BIGINT NOT NULL, bytes_out BIGINT NOT NULL,
    decision TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS builder_network_log_scope ON
    builder_network_log(lane, owner, conversation_id, id DESC);
