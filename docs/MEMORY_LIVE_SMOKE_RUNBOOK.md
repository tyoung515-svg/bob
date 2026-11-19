# Memory checks

There are two different memory paths. New runtime lanes project verbatim turns
from SQLite events into Qdrant and record writer completions. Optional fact
extraction is a separate path and is disabled in those lanes. A test of one does
not verify the other.

## Runtime lane check

Use a disposable lane with its normal provider and optional embedder configured.
A chat can incur provider usage and creates durable records, so use harmless,
unique text rather than secrets.

1. Record the runtime version, lane, configured memory database and embedding
   selection. The database is normally the lane's `bobclaw_memory.db`.
   Runtime embedding slots use the root-owned `/etc/bob/embedder.env` selection
   and loopback server on 58081. Standalone source setup starts with memory off.
2. Send a harmless fact in one desktop conversation and wait for the turn and
   background writer to finish. Open another conversation and ask about that
   fact. A plausible answer alone is not proof of retrieval.
3. Inspect the lane database read-only. If the `sqlite3` client is available,
   substitute the actual lane in the quoted path and run inside the runtime as an authorized
   operator:

   ```bash
   memory_db="/var/lib/bob/family/<lane>/bob-memory/bobclaw_memory.db"
   sqlite3 -readonly "$memory_db" \
     'SELECT event_id,kind FROM memory_events ORDER BY insertion_order DESC LIMIT 10;'
   sqlite3 -readonly "$memory_db" \
     'SELECT source_event_id,status,chunk_count FROM memory_writer_completions ORDER BY updated_at DESC LIMIT 10;'
   ```

   Replace `<lane>` before using these commands; it is not shell syntax.
   Match the relevant event with a writer completion. Repeated identical
   content can reuse an earlier completed claim, so do not expect one new
   completion or vector per turn.
4. Inspect the configured Qdrant collection and memory/retrieval results for
   the same content. A collection count alone does not prove the right content
   was indexed or retrieved. Record missing events, failed completions and
   unavailable-embedder warnings separately from chat success.

Do not modify the event log, delete facts/vectors or change embedding dimensions
to make a smoke check pass. Preserve the test records as normal lane data.

## Optional fact-extraction integration fixture

`bobclaw-core/tests/test_memory_l1_extraction_smoke.py` uses a live embedder and
Qdrant, but **stubs the extractor and conversation model**. It uses temporary
SQLite databases and unique throwaway Qdrant collections. It checks full-loop
storage/retrieval, extraction-disabled behavior and repeated-turn dedup.

This fixture retains a 768-dimensional setup: it probes
`MEMORY_EMBEDDER_URL` (default `http://localhost:8081`) and tears down a collection
whose name ends in `_768`. The shipped slot files instead select Qwen embedding
dimensions and defer `extract_small`. Running with those defaults is not a
valid live fact-extraction test.

Use a disposable test environment with a matching 768-dimensional embedder and
an explicit `MEMORY_SLOTS_FILE` overlay. Its active `embed_text` entry must match
the test server's model, endpoint and dimension; `extract_small` must be an active
`lmstudio` entry so resolution can reach the stub. `MEMORY_EMBEDDER_URL` only
changes the health probe, not the slot endpoint. Do not change production slots
or use a production Qdrant for this fixture.

After preparing that environment, from `bobclaw-core`:

```bash
MEMORY_QDRANT_URL=http://127.0.0.1:6353 \
MEMORY_EMBEDDER_URL=http://127.0.0.1:8081 \
MEMORY_SLOTS_FILE=/absolute/path/to/test-memory-slots.toml \
  ../.venv/bin/python -m pytest -m integration \
  tests/test_memory_l1_extraction_smoke.py -v
```

The helper refuses port 6333 without an explicit override; keep the default
test endpoint on 6353. Missing live services can skip tests. Check the actual
result, teardown and absence of throwaway residue; no expected pass count is
given here. The helper compares non-test collection names and point counts,
which does not prove their contents stayed byte-for-byte identical.

The fixture does not populate the normal lane database, so querying a production
database after it finishes cannot verify its assertions. It also does not prove
a live extraction model's quality or the runtime's verbatim writer.

## Offline checks

Use the development environment and suite commands in
[CONTRIBUTING.md](../CONTRIBUTING.md). Record the revision, configuration, commands,
results/skips and whether a real model was called. See
[deduplication](MEMORY_DEDUP_DESIGN.md) and
[version changes](MEMORY_VERSION_BUMP_RUNBOOK.md) before changing extraction.
