-- S4f/4.4. Failure control is scoped and committed with attempts/events/flags.
CREATE TABLE IF NOT EXISTS agent_failure_episodes (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, ordinal INTEGER NOT NULL, binding JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('backoff','audit','guided','paused','resolved')),
    resume_state TEXT CHECK (resume_state IN ('backoff','audit','guided')),
    basic_retries INTEGER NOT NULL DEFAULT 0 CHECK (basic_retries BETWEEN 0 AND 2),
    audit_calls INTEGER NOT NULL DEFAULT 0 CHECK (audit_calls BETWEEN 0 AND 3),
    guided_calls INTEGER NOT NULL DEFAULT 0 CHECK (guided_calls BETWEEN 0 AND 1),
    retry_at TIMESTAMPTZ, effect_class TEXT NOT NULL,
    effect_status TEXT NOT NULL CHECK (effect_status IN ('none','applied','not_applied','unknown')),
    auditor TEXT, audit_binding JSONB, audit_input JSONB, audit JSONB, raw_audit TEXT,
    last_failure JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lane,owner,run_id,id), UNIQUE (lane,owner,run_id,fingerprint),
    FOREIGN KEY (lane,owner,run_id,ordinal) REFERENCES agent_run_steps(lane,owner,run_id,ordinal),
    CHECK (effect_class IN ('read_only','idempotent_by_key','workspace_mutation','external_or_unknown'))
);
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS failure_episode_id TEXT;
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS effect_class TEXT NOT NULL DEFAULT 'read_only';
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS effect_receipt JSONB;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS failure_episode_id TEXT;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'initial';
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS failure_detail JSONB;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS response TEXT;
ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS response_usage JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS agent_failure_inflight
    ON agent_attempts(lane,owner,run_id,failure_episode_id)
    WHERE failure_episode_id IS NOT NULL AND state IN ('reserved','dispatched');
ALTER TABLE agent_attempts DROP CONSTRAINT IF EXISTS agent_attempt_failure_episode;
ALTER TABLE agent_attempts ADD CONSTRAINT agent_attempt_failure_episode
    FOREIGN KEY(lane,owner,run_id,failure_episode_id) REFERENCES agent_failure_episodes(lane,owner,run_id,id);
ALTER TABLE agent_run_steps DROP CONSTRAINT IF EXISTS agent_step_failure_episode;
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_step_failure_episode
    FOREIGN KEY(lane,owner,run_id,failure_episode_id) REFERENCES agent_failure_episodes(lane,owner,run_id,id);
CREATE TABLE IF NOT EXISTS agent_flags (
    lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
    episode_key TEXT NOT NULL, episode_id TEXT, ordinal INTEGER,
    cause TEXT NOT NULL, phase TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    resolution JSONB, context JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lane,owner,id), UNIQUE (lane,owner,run_id,episode_key),
    FOREIGN KEY (lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
    FOREIGN KEY (lane,owner,run_id,episode_id) REFERENCES agent_failure_episodes(lane,owner,run_id,id)
);
CREATE INDEX IF NOT EXISTS agent_flags_run ON agent_flags(lane,owner,run_id,status);
-- The existing queue owns worker reconciliation. Surface its unknown transition
-- atomically without changing that queue or granting a replay/resume capability.
CREATE OR REPLACE FUNCTION agent_worker_unknown_flag() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE flag_id TEXT;
BEGIN
    IF NEW.status='paused' AND NEW.reason='worker_outcome_unknown' THEN
        flag_id := md5(NEW.lane || ':' || NEW.owner || ':' || NEW.id || ':worker_outcome_unknown');
        INSERT INTO agent_flags(lane,owner,run_id,id,episode_key,cause,phase,context)
        VALUES(NEW.lane,NEW.owner,NEW.id,flag_id,'worker_outcome_unknown',NEW.reason,NEW.phase,
               jsonb_build_object('initial_budget',NEW.initial_budget,'request',NEW.request,
                   'selected_grant_id',NEW.selected_grant_id,'selected_grant_revision',NEW.selected_grant_revision,
                   'account',(SELECT to_jsonb(a) - 'lane' - 'owner' FROM agent_budget_accounts a
                              WHERE (a.lane,a.owner,a.run_id)=(NEW.lane,NEW.owner,NEW.id))))
        ON CONFLICT (lane,owner,run_id,episode_key) DO NOTHING;
        IF FOUND THEN
            NEW.event_seq := NEW.event_seq+1;
            NEW.revision := NEW.revision+1;
            INSERT INTO agent_run_events(lane,owner,run_id,seq,kind,detail)
            VALUES(NEW.lane,NEW.owner,NEW.id,NEW.event_seq,'flag_raised',jsonb_build_object('flag_id',flag_id,'cause',NEW.reason));
        END IF;
        NEW.owner_flag := EXISTS(SELECT 1 FROM agent_flags f WHERE (f.lane,f.owner,f.run_id)=(NEW.lane,NEW.owner,NEW.id) AND f.status='open');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS agent_worker_unknown_flag ON agent_runs;
CREATE TRIGGER agent_worker_unknown_flag BEFORE UPDATE OF status,reason ON agent_runs
FOR EACH ROW EXECUTE FUNCTION agent_worker_unknown_flag();
-- Existing 4.3a unknowns are already paused and will not be claimed by the pump.
-- Re-enter only the projection trigger, not execution or reconciliation.
UPDATE agent_runs SET status=status WHERE status='paused' AND reason='worker_outcome_unknown';
