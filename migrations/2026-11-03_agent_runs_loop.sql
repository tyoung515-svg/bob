-- S4d/4.2: text-only loop, transferable allocations, optional owner-set pools.
ALTER TABLE agent_budget_accounts DROP CONSTRAINT IF EXISTS agent_budget_accounts_call_limit_check;
ALTER TABLE agent_budget_accounts ADD CONSTRAINT agent_budget_accounts_call_limit_check CHECK (call_limit > 0);
ALTER TABLE agent_run_steps DROP CONSTRAINT IF EXISTS agent_run_steps_ordinal_check;
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_run_steps_ordinal_check CHECK (ordinal >= 0);
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS spec JSONB;
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS checks JSONB;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS allocation_id TEXT NOT NULL DEFAULT 'root';
ALTER TABLE agent_attempts DROP CONSTRAINT IF EXISTS agent_attempts_lane_owner_run_id_ordinal_key;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS loop_state JSONB NOT NULL DEFAULT '{}';
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS owner_flag BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS wall_rule_seconds BIGINT NOT NULL DEFAULT 360;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'dispatch';
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS initial_budget JSONB;
UPDATE agent_runs SET initial_budget=request->'budget' WHERE initial_budget IS NULL;

CREATE TABLE IF NOT EXISTS agent_call_pools (
    lane TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('project','sprint')),
    id TEXT NOT NULL, call_limit BIGINT NOT NULL CHECK (call_limit > 0),
    consumed BIGINT NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    reserved BIGINT NOT NULL DEFAULT 0 CHECK (reserved >= 0), revision BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (lane,owner,kind,id), CHECK (consumed <= call_limit - reserved)
);
CREATE TABLE IF NOT EXISTS agent_pool_holds (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL, pool_id TEXT NOT NULL,
    remaining BIGINT NOT NULL CHECK (remaining >= 0),
    PRIMARY KEY (lane,owner,run_id,kind,pool_id),
    FOREIGN KEY (lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
    FOREIGN KEY (lane,owner,kind,pool_id) REFERENCES agent_call_pools(lane,owner,kind,id)
);
CREATE TABLE IF NOT EXISTS agent_budget_allocations (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL, parent_id TEXT,
    allowance BIGINT NOT NULL CHECK (allowance >= 0),
    consumed BIGINT NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    reserved BIGINT NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    delegated BIGINT NOT NULL DEFAULT 0 CHECK (delegated >= 0),
    PRIMARY KEY (lane,owner,run_id,id),
    FOREIGN KEY (lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
    FOREIGN KEY (lane,owner,run_id,parent_id) REFERENCES agent_budget_allocations(lane,owner,run_id,id),
    CHECK (consumed <= allowance - reserved - delegated)
);
-- No grant write API. Slice 3 owns issuance; tests insert scoped records directly.
CREATE TABLE IF NOT EXISTS agent_execution_grants (
    lane TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 1,
    run_id TEXT, project_id TEXT, authority_boundary TEXT NOT NULL,
    task_digest TEXT NOT NULL, team_digest TEXT NOT NULL, models_digest TEXT NOT NULL,
    allow_long_runs BOOLEAN NOT NULL DEFAULT FALSE, auto_escalate BOOLEAN NOT NULL DEFAULT TRUE,
    call_ceiling BIGINT CHECK (call_ceiling > 0), revoked BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (lane,owner,id), CHECK (run_id IS NOT NULL OR project_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS agent_accepted_outputs (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, digest TEXT NOT NULL, content TEXT NOT NULL,
    criteria_ids JSONB NOT NULL, check_receipts JSONB NOT NULL,
    PRIMARY KEY (lane,owner,run_id,id), UNIQUE (lane,owner,run_id,ordinal),
    FOREIGN KEY (lane,owner,run_id,ordinal) REFERENCES agent_run_steps(lane,owner,run_id,ordinal)
);
-- Upgrade existing 4.1 roots without changing prior consumption.
INSERT INTO agent_budget_allocations(lane,owner,run_id,id,allowance,consumed)
SELECT lane,owner,run_id,'root',call_limit,consumed FROM agent_budget_accounts
ON CONFLICT DO NOTHING;
