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
