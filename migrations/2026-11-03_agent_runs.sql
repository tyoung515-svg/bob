-- S4b/4.1. All resource references include lane + owner. No legacy JSON import.
CREATE TABLE IF NOT EXISTS agent_team_heads (
    lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    PRIMARY KEY (lane, owner, id)
);
CREATE TABLE IF NOT EXISTS agent_team_versions (
    lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL, revision BIGINT NOT NULL,
    actor TEXT NOT NULL, boundary TEXT NOT NULL, definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lane, owner, id, revision),
    FOREIGN KEY (lane, owner, id) REFERENCES agent_team_heads (lane, owner, id)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
    actor TEXT NOT NULL, boundary TEXT NOT NULL, run_authority JSONB NOT NULL,
    authorization_revoked BOOLEAN NOT NULL DEFAULT FALSE,
    team TEXT NOT NULL, team_revision BIGINT NOT NULL, snapshot JSONB NOT NULL,
    request JSONB NOT NULL, status TEXT NOT NULL, reason TEXT,
    revision BIGINT NOT NULL DEFAULT 1, event_seq BIGINT NOT NULL DEFAULT 0,
    deadline TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    controller TEXT, generation BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ,
    PRIMARY KEY (lane, owner, id),
    FOREIGN KEY (lane, owner, team, team_revision)
        REFERENCES agent_team_versions (lane, owner, id, revision),
    CHECK (status IN ('accepted','queued','running','recovering','paused','cancelling','completed','cancelled','failed'))
);
CREATE INDEX IF NOT EXISTS agent_runs_pump ON agent_runs (lane, status, lease_until);
CREATE TABLE IF NOT EXISTS agent_budget_accounts (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
    call_limit BIGINT NOT NULL CHECK (call_limit >= 4),
    consumed BIGINT NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    unknown_calls BIGINT NOT NULL DEFAULT 0 CHECK (unknown_calls >= 0),
    PRIMARY KEY (lane, owner, run_id),
    FOREIGN KEY (lane, owner, run_id) REFERENCES agent_runs (lane, owner, id),
    CHECK (consumed <= call_limit AND unknown_calls <= consumed)
);
CREATE TABLE IF NOT EXISTS agent_run_steps (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 3), role TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready', output TEXT, output_digest TEXT, usage JSONB,
    PRIMARY KEY (lane, owner, run_id, ordinal),
    FOREIGN KEY (lane, owner, run_id) REFERENCES agent_runs (lane, owner, id)
);
CREATE TABLE IF NOT EXISTS agent_attempts (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
    id TEXT NOT NULL, ordinal INTEGER NOT NULL, generation BIGINT NOT NULL,
    binding JSONB NOT NULL, input_digest TEXT NOT NULL, state TEXT NOT NULL,
    dispatched_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lane, owner, id),
    UNIQUE (lane, owner, run_id, ordinal),
    FOREIGN KEY (lane, owner, run_id, ordinal)
        REFERENCES agent_run_steps (lane, owner, run_id, ordinal)
);
CREATE TABLE IF NOT EXISTS agent_run_events (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
    seq BIGINT NOT NULL, kind TEXT NOT NULL, detail JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lane, owner, run_id, seq),
    FOREIGN KEY (lane, owner, run_id) REFERENCES agent_runs (lane, owner, id)
);
CREATE TABLE IF NOT EXISTS agent_mutations (
    lane TEXT NOT NULL, owner TEXT NOT NULL, operation TEXT NOT NULL, request_key TEXT NOT NULL,
    boundary TEXT NOT NULL, payload_hash TEXT NOT NULL, resource_id TEXT NOT NULL,
    operation_id TEXT NOT NULL, result JSONB NOT NULL,
    PRIMARY KEY (lane, owner, operation, request_key)
);
