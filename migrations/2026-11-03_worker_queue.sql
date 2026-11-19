-- 4.3a: execution authority stays in core. Worker history is recovery evidence.
CREATE TABLE IF NOT EXISTS worker_callers (
 lane TEXT NOT NULL, owner TEXT NOT NULL, principal TEXT NOT NULL, caller_group TEXT NOT NULL,
 boundary TEXT NOT NULL, last_served BIGINT NOT NULL DEFAULT 0,
 paused_unknown BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(lane,owner,principal,caller_group)
);
CREATE TABLE IF NOT EXISTS worker_job_attempts (
 lane TEXT NOT NULL, owner TEXT NOT NULL, execution_id TEXT NOT NULL,
 principal TEXT NOT NULL, caller_group TEXT NOT NULL, logical_job_id TEXT NOT NULL,
 boundary TEXT NOT NULL,
 run_id TEXT, run_generation BIGINT, request JSONB NOT NULL, admission_digest TEXT NOT NULL, request_digest TEXT,
 selected_seconds DOUBLE PRECISION NOT NULL CHECK(selected_seconds BETWEEN 0.1 AND 900),
 effective_seconds DOUBLE PRECISION CHECK(effective_seconds BETWEEN 0.1 AND 900),
 lane_generation BIGINT, controller_session TEXT, daemon_epoch TEXT,
 outcome TEXT NOT NULL DEFAULT 'queued' CHECK(outcome IN
 ('queued','dispatch_intent','running','succeeded','failed','timed_out','cancelled','unknown')),
 cleanup TEXT NOT NULL DEFAULT 'not_started' CHECK(cleanup IN ('not_started','pending','idle_proved','unknown')),
 reconciliation TEXT NOT NULL DEFAULT 'pending' CHECK(reconciliation IN ('pending','established','unknown','conflict')),
 cancel_requested BOOLEAN NOT NULL DEFAULT FALSE, receipt JSONB,
 queued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 dispatched_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
 PRIMARY KEY(lane,execution_id), UNIQUE(lane,owner,execution_id),
 FOREIGN KEY(lane,owner,principal,caller_group) REFERENCES worker_callers(lane,owner,principal,caller_group),
 FOREIGN KEY(lane,owner,run_id) REFERENCES agent_runs(lane,owner,id),
 CHECK ((run_id IS NULL) = (run_generation IS NULL))
);
CREATE TABLE IF NOT EXISTS worker_queue (
 ticket BIGSERIAL PRIMARY KEY, lane TEXT NOT NULL, owner TEXT NOT NULL,
 execution_id TEXT NOT NULL, UNIQUE(lane,execution_id),
 FOREIGN KEY(lane,owner,execution_id) REFERENCES worker_job_attempts(lane,owner,execution_id)
);
CREATE TABLE IF NOT EXISTS worker_job_events (
 seq BIGSERIAL PRIMARY KEY, lane TEXT NOT NULL, owner TEXT NOT NULL, execution_id TEXT NOT NULL,
 kind TEXT NOT NULL, detail JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(lane,owner,execution_id) REFERENCES worker_job_attempts(lane,owner,execution_id)
);
CREATE TABLE IF NOT EXISTS worker_job_dedup (
 lane TEXT NOT NULL, owner TEXT NOT NULL, execution_id TEXT NOT NULL, request_digest TEXT NOT NULL,
 PRIMARY KEY(lane,execution_id),
 FOREIGN KEY(lane,owner,execution_id) REFERENCES worker_job_attempts(lane,owner,execution_id)
);
CREATE TABLE IF NOT EXISTS worker_lane_slots (
 lane TEXT PRIMARY KEY, generation BIGINT NOT NULL DEFAULT 0, controller_session TEXT,
 active_execution TEXT, daemon_epoch TEXT, cleanup TEXT NOT NULL DEFAULT 'unknown',
 FOREIGN KEY(lane,active_execution) REFERENCES worker_job_attempts(lane,execution_id)
);
CREATE INDEX IF NOT EXISTS worker_jobs_principal ON worker_job_attempts(lane,principal,outcome);
