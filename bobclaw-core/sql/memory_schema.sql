CREATE TABLE IF NOT EXISTS memory_events (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    body_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    hash TEXT NOT NULL,
    prev_hash TEXT,
    insertion_order INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_events_insertion_order
    ON memory_events(insertion_order);

CREATE TABLE IF NOT EXISTS memory_facts (
    fact_id TEXT PRIMARY KEY,
    generation_method TEXT NOT NULL,
    body_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    confidence_json TEXT NOT NULL,
    ts TEXT NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES memory_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_source_event_id
    ON memory_facts(source_event_id);

-- W3 writer completion ledger.  The three-part primary key is the projection
-- identity: content changes, task-version changes, and prompt-version changes
-- are all independently replayable.  RUNNING is a recoverable claim; vectors
-- use deterministic ids, so reclaiming a stale row is safe.
CREATE TABLE IF NOT EXISTS memory_writer_completions (
    source_content_hash TEXT NOT NULL,
    task_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    task_name TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (source_content_hash, task_version, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_memory_writer_completion_event
    ON memory_writer_completions(source_event_id, task_name, status);

CREATE TABLE IF NOT EXISTS memory_writer_checkpoints (
    task_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    processed_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_version, prompt_version),
    FOREIGN KEY (last_event_id) REFERENCES memory_events(event_id)
);
