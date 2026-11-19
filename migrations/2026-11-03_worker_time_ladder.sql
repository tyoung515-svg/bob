-- 4.3b: old grant versions gain no permissions. Explicit v3 reviewed job scope.
ALTER TABLE agent_execution_grants DROP CONSTRAINT IF EXISTS agent_execution_grants_matcher_version_check;
ALTER TABLE agent_execution_grants ADD CONSTRAINT agent_execution_grants_matcher_version_check CHECK(matcher_version IN (1,2,3,4));
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS job_coverage JSONB;
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS job_override_seconds BIGINT CHECK(job_override_seconds > 0 AND job_override_seconds <= 2147483647);
ALTER TABLE agent_execution_grants ADD COLUMN IF NOT EXISTS job_auto_escalate BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE worker_job_attempts DROP CONSTRAINT IF EXISTS worker_job_attempts_selected_seconds_check;
ALTER TABLE worker_job_attempts DROP CONSTRAINT IF EXISTS worker_job_attempts_effective_seconds_check;
ALTER TABLE worker_job_attempts ADD CONSTRAINT worker_job_attempts_selected_seconds_check CHECK(selected_seconds BETWEEN 0.1 AND 2147483647);
ALTER TABLE worker_job_attempts ADD CONSTRAINT worker_job_attempts_effective_seconds_check CHECK(effective_seconds BETWEEN 0.1 AND 2147483647);
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS proposed_seconds DOUBLE PRECISION;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS rationale TEXT NOT NULL DEFAULT 'legacy bounded tool request';
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS owner_override BIGINT;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS auto_escalate BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS explicit_policy BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS custom_limit BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS parent_execution_id TEXT;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS timeout_episode BIGINT NOT NULL DEFAULT 0;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS grant_id TEXT;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS grant_revision BIGINT;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS limiting_reason TEXT;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS reconciliation_evidence JSONB;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS execution_seconds DOUBLE PRECISION;
ALTER TABLE worker_job_attempts ADD COLUMN IF NOT EXISTS cleanup_seconds DOUBLE PRECISION;
ALTER TABLE worker_job_attempts DROP CONSTRAINT IF EXISTS worker_job_parent_fk;
ALTER TABLE worker_job_attempts ADD CONSTRAINT worker_job_parent_fk FOREIGN KEY(lane,owner,parent_execution_id) REFERENCES worker_job_attempts(lane,owner,execution_id);
ALTER TABLE worker_job_attempts DROP CONSTRAINT IF EXISTS worker_job_grant_fk;
ALTER TABLE worker_job_attempts ADD CONSTRAINT worker_job_grant_fk FOREIGN KEY(lane,owner,grant_id) REFERENCES agent_execution_grants(lane,owner,id);
CREATE UNIQUE INDEX IF NOT EXISTS worker_job_continuation_once ON worker_job_attempts(lane,parent_execution_id) WHERE parent_execution_id IS NOT NULL;
ALTER TABLE worker_callers ADD COLUMN IF NOT EXISTS paused_timeout TEXT;
CREATE TABLE IF NOT EXISTS run_job_accounts (
 lane TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
 max_job_attempts BIGINT NOT NULL CHECK(max_job_attempts > 0),
 job_seconds_budget BIGINT CHECK(job_seconds_budget > 0),
 dispatched BIGINT NOT NULL DEFAULT 0, held BIGINT NOT NULL DEFAULT 0,
 completed BIGINT NOT NULL DEFAULT 0, unknown BIGINT NOT NULL DEFAULT 0,
 seconds_used DOUBLE PRECISION NOT NULL DEFAULT 0, seconds_held DOUBLE PRECISION NOT NULL DEFAULT 0,
 cleanup_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
 cli_usage_basis TEXT NOT NULL DEFAULT 'estimated_or_unknown',
 PRIMARY KEY(lane,owner,run_id),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
 CHECK(dispatched >= 0 AND held >= 0 AND completed >= 0 AND unknown >= 0),
 CHECK(seconds_used >= 0 AND seconds_held >= 0 AND cleanup_seconds >= 0)
);
-- Existing runs retain finite job authority. Earlier dispatches are counted,
-- uncertain executions retain their full time hold.
INSERT INTO run_job_accounts(lane,owner,run_id,max_job_attempts,dispatched,held,completed,unknown,seconds_held,seconds_used)
SELECT r.lane,r.owner,r.id,greatest(32,count(a.execution_id)),
 count(a.dispatched_at),count(*) FILTER(WHERE a.outcome IN ('dispatch_intent','running','unknown')),
 count(*) FILTER(WHERE a.dispatched_at IS NOT NULL AND a.outcome IN ('succeeded','failed','timed_out','cancelled')),
 count(*) FILTER(WHERE a.outcome='unknown'),
 coalesce(sum(a.effective_seconds) FILTER(WHERE a.outcome IN ('dispatch_intent','running','unknown')),0),
 coalesce(sum(a.effective_seconds) FILTER(WHERE a.dispatched_at IS NOT NULL AND a.outcome IN ('succeeded','failed','timed_out','cancelled')),0)
FROM agent_runs r LEFT JOIN worker_job_attempts a ON (a.lane,a.owner,a.run_id)=(r.lane,r.owner,r.id)
GROUP BY r.lane,r.owner,r.id ON CONFLICT DO NOTHING;
