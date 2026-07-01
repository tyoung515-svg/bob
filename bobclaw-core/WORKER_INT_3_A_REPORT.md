---
owner: personal
---

# WORKER INT-3-A REPORT

## Summary

Moved L1 fact-extraction from inline sync in `_append_agent_turn_event` to background `asyncio.create_task`. The L0 `atomic_append` remains synchronous; the three-step extraction pipeline (extract → put → reindex) now runs as a tracked async task. Extraction failures are recorded on `MemorySingletons.last_extraction_error` and logged, but never propagate to the user-facing response path.

## Files changed

| File | Change |
|---|---|
| `bobclaw-core/core/memory/bootstrap.py` | `MemorySingletons`: `frozen=False`, added `pending_extraction_tasks`, `last_extraction_error`, `drain_extraction_tasks()` method |
| `bobclaw-core/core/nodes/_l0_events.py` | Wrapped L1 block in `asyncio.create_task(_run_l1_extraction(...))`; added `_run_l1_extraction` helper with try/except that records+logs |
| `bobclaw-core/tests/test_memory_l1_extraction_smoke.py` | Inserted `await mem.drain_extraction_tasks()` before 3 L1-state assertions |
| `bobclaw-core/tests/memory/test_l1_extraction_async.py` | **NEW** — 10 tests for async extraction behavior |

## Test results

### New tests
```
tests/memory/test_l1_extraction_async.py::test_agent_turn_returns_before_extraction_completes PASSED
tests/memory/test_l1_extraction_async.py::test_extraction_task_is_registered_on_singletons PASSED
tests/memory/test_l1_extraction_async.py::test_drain_extraction_tasks_awaits_completion PASSED
tests/memory/test_l1_extraction_async.py::test_extraction_failure_does_not_propagate PASSED
tests/memory/test_l1_extraction_async.py::test_l0_event_durable_when_extraction_fails PASSED
tests/memory/test_l1_extraction_async.py::test_extraction_disabled_skips_task PASSED
tests/memory/test_l1_extraction_async.py::test_two_concurrent_turns_register_two_tasks PASSED
tests/memory/test_l1_extraction_async.py::test_task_is_removed_from_set_on_completion PASSED
tests/memory/test_l1_extraction_async.py::test_last_extraction_error_replaced_on_subsequent_failure PASSED
tests/memory/test_l1_extraction_async.py::test_successful_extraction_does_not_set_last_error PASSED
```

### Memory suite
```
382 passed in 3.59s
```

### Core suite
```
713 passed, 6 skipped, 1 warning in 9.50s
```

### Multi-process race test
```
test_atomic_append_under_multi_process_load PASSED
```

### Hard Rule checks
- Model names in changed files: **0 matches** (grep for all known model prefixes returned empty)
- `except Exception:\s*pass`: **0 matches** in `_l0_events.py`, `bootstrap.py`
- Structured exception in `_run_l1_extraction`: `except Exception as exc:` followed by `singletons.last_extraction_error = exc` + `logger.exception(...)` — both recorded AND logged, per prompt authorization

## Commit SHAs (from INT-3 kickoff)
```
bd382c0 INT-3-B: connection() timeout consolidation + _latest_event_hash removal
38af01a INT-3: pin baselines at kickoff
```

## git status --short at report time
```
 M bobclaw-core/core/memory/bootstrap.py
 M bobclaw-core/core/nodes/_l0_events.py
 M bobclaw-core/tests/test_memory_l1_extraction_smoke.py
?? bobclaw-core/tests/memory/test_l1_extraction_async.py
?? bobclaw-core/WORKER_INT_3_A_REPORT.md
```

## Deviations from file inventory
None. All 4 files match the prompt's inventory exactly.

## Model name check
`grep -nE 'granite-|gemma-|qwen-|claude-|kimi-|gpt-|gemini-|llama-|deepseek-'` on new/changed files returned empty for all paths.
