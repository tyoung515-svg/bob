---
owner: personal
---

# Memory Module — Live LMStudio + Qdrant Smoke Runbook

## 1. Purpose

One-time live verification of the L1 fact-extraction pipeline against real LMStudio (embedding + extraction models) and real Qdrant. This resolves SPRINT INT-2 Deep Audit Check 2's recommendation, which states (verbatim):

> *"Recommend a one-time live verification before any production-multi-process flip: start LMStudio with granite-embedding-311m loaded, run `pytest -m integration tests/test_memory_l1_extraction_smoke.py -v`, confirm 3/3 pass."*

Run this before any production multi-process flip and after any change to embedder, extractor, slot config, or DB schema.

## 2. Prerequisites

- [ ] **LMStudio** is running on `http://localhost:1234` with both required models loaded:

  ```bash
  curl -s http://localhost:1234/v1/models | jq '.data[].id'
  ```

  Expected output MUST include BOTH:
  - `granite-embedding-311m` (embed_text slot)
  - `gemma-4-e4b-it` (extract_small slot)

  If either model is missing, load it via LMStudio's model manager and wait for the "loaded" indicator before proceeding.

- [ ] **Qdrant** is running on `localhost:6333`:

  ```bash
  curl -s http://localhost:6333/healthz
  ```

  Expected output: a health-check response (typically empty body with status 200, or `{"ok":true}`). If connection refused, start Qdrant: `docker run -d -p 6333:6333 qdrant/qdrant` (or systemd service equivalent).

- [ ] **Working tree is clean** on `bob5/BoBClaw/`:

  ```bash
  cd C:\Users\tyoun\Desktop\bob5\BoBClaw
  git status --short
  ```

  Expected: empty output. HEAD is the version under test.

- [ ] **Python environment** has all dependencies installed:

  ```bash
  cd bobclaw-core
  python -c "import qdrant_client; import aiohttp; import aiosqlite"
  ```

  If any import fails, run `pip install -r requirements.txt` (or the project's equivalent install command).

## 3. Run

```bash
cd C:\Users\tyoun\Desktop\bob5\BoBClaw\bobclaw-core
python -m pytest -m integration tests/test_memory_l1_extraction_smoke.py -v
```

## 4. Expected output

```
collected 3 items

tests/test_memory_l1_extraction_smoke.py::TestL1ExtractionSmoke::test_l1_extraction_smoke_full_loop PASSED
tests/test_memory_l1_extraction_smoke.py::TestL1ExtractionSmoke::test_l1_extraction_disabled_no_facts PASSED
tests/test_memory_l1_extraction_smoke.py::TestL1ExtractionSmoke::test_l1_extraction_dedup_across_turns PASSED

============ 3 passed in <~10-30s> ============
```

Wall time depends on LMStudio inference speed and Qdrant latency. Typical range: 10–30 seconds for the full suite.

## 5. Failure modes

| Symptom | Root cause | Remediation |
|---|---|---|
| `EmbedderUnavailable` | LMStudio not running or `granite-embedding-311m` not loaded | Run prerequisite check (step 2). Verify LMStudio is serving on `localhost:1234` and the model is listed in `/v1/models`. Load model via LMStudio UI, then retry. |
| `SlotMisconfigured` | `config/memory_slots.toml` model names don't match the models loaded in LMStudio | `diff` the slot file against the canonical INT-3 version (check `git diff config/memory_slots.toml`). If the slot model name was changed, either revert or load the correct model in LMStudio. |
| `qdrant_client` connection refused | Qdrant not running on `localhost:6333` | Start Qdrant (`docker run -d -p 6333:6333 qdrant/qdrant` or systemctl). Verify with the prerequisite check, then retry. |
| Test hang (pytest doesn't return) | LMStudio is loading the model on first use (cold-start delay) | Wait up to 60 seconds. If still hung, abort (`Ctrl+C`), ensure the model shows in `/v1/models`, restart LMStudio, then retry once. If hangs persist, restart LMStudio entirely. |
| `prev_hash mismatch` assertion failure | Multi-process race in L0 event chain | Should not occur post-INT-2 (BEGIN IMMEDIATE semantics in `atomic_append`). If it does, PAGE the operator — this is a regression in the lock semantics. |
| `FAILED test_l1_extraction_disabled_no_facts` | Extraction running when MEMORY_L1_EXTRACTION_ENABLED is false — config mock may not be respected | Check `monkeypatch` call in the test fixture hasn't been broken by a config refactor. Verify `core.config.config.MEMORY_L1_EXTRACTION_ENABLED` eval at test time. |
| `FAILED test_l1_extraction_dedup_across_turns` | Dedup hash broken or version mismatch | Check `_EXTRACTOR_VERSION` / `_PROMPT_VERSION` in `extractor.py` haven't been bumped without corresponding test update. Verify the dedup query in `_dedup_and_build_facts` spans all expected turns. |

## 6. Post-run verification

After a successful run, verify L0 and L1 state on disk:

**L0 event count:**
```bash
sqlite3 bobclaw-core/.memory/bobclaw_memory.db "SELECT COUNT(*) FROM memory_events;"
```
(Path is `MemoryBootstrapConfig.sqlite_path` default — override if `MEMORY_SQLITE_PATH` is set.)

Expected: 3 events (one per agent_turn: turn1, turn2 from `full_loop`, one from `dedup_across_turns`; the `disabled_no_facts` test uses a separate database at a unique tmp_path).

**L1 fact count:**
```bash
sqlite3 bobclaw-core/.memory/bobclaw_memory.db "SELECT COUNT(*) FROM memory_facts;"
```
Expected: at least 1 fact from the full-loop event, plus dedup-limited facts from the dedup test turn.

**Qdrant collection:**
```bash
curl -s http://localhost:6333/collections | jq '.result.collections[].name'
```
The collection name is dynamically computed as `{collection_prefix}_{dim}` per `core/memory/providers/qdrant_provider.py:55-56`. With `collection_prefix="bobclaw_"` (from `config/memory_stores.toml:10`) and `embedding_dimension=768` (from `config/memory_slots.toml:24`), the live name is typically `bobclaw__768`. To identify it robustly:

```bash
curl -s http://localhost:6333/collections | jq '.result.collections[].name' | grep bobclaw
```

Then query the identified collection (substitute the actual name from the output):
```bash
curl -s http://localhost:6333/collections/<name-from-above> | jq '.result.points_count'
```
Expected: non-zero points count matching the number of fact chunks indexed during the test suite.

## 7. When to run this

- [ ] **Before any production multi-process flip** — one-time gate; this is the primary trigger from INT-2 audit Check 2.
- [ ] **After any change to the embedder** (model swap, backend swap, slot config change).
- [ ] **After any change to the extractor** (prompt template, version constant, dedup logic, LLM call).
- [ ] **After any change to slot config** (`config/memory_slots.toml` — model names, backends, endpoints, embedding_dimension).
- [ ] **After any change to DB schema** (`sql/memory_schema.sql` — table structure, index changes).
- [ ] **After any change to Qdrant provider** (`core/memory/providers/qdrant_provider.py` — index/query/scroll logic).

## 8. Sign-off

The operator records the run result in the close kit of the sprint that motivated the run.

```markdown
### Live smoke verification
- **Date:** YYYY-MM-DD
- **Operator:** the repo owner
- **HEAD:** `<full-commit-sha>`
- **test_l1_extraction_smoke_full_loop:** PASS / FAIL
- **test_l1_extraction_disabled_no_facts:** PASS / FAIL
- **test_l1_extraction_dedup_across_turns:** PASS / FAIL
- **Result:** PASS / FAIL
- **Notes:** <any deviations, workarounds, or observations>
```
