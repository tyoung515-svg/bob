from __future__ import annotations

import json
import threading
from pathlib import Path

from core.memory.query_log import QueryLog


def test_append_writes_one_json_line(tmp_path: Path):
    log = QueryLog(tmp_path / "query_log.jsonl")
    log.append({"query": "hello", "k": 3})
    lines = (tmp_path / "query_log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["query"] == "hello"


def test_append_round_trip(tmp_path: Path):
    log = QueryLog(tmp_path / "query_log.jsonl")
    record = {"query": "test", "k": 5, "hop_budget": 1, "max_nodes": 1}
    log.append(record)
    lines = (tmp_path / "query_log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    parsed = json.loads(lines[0])
    assert parsed == record


def test_multiprocess_safety(tmp_path: Path):
    log = QueryLog(tmp_path / "query_log.jsonl")

    def append_thread(idx: int):
        log.append({"query": f"q{idx}", "k": idx})

    threads = [threading.Thread(target=append_thread, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = (tmp_path / "query_log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 10


def test_record_contains_required_fields(tmp_path: Path):
    log = QueryLog(tmp_path / "query_log.jsonl")
    log.append({
        "ts": "2026-01-01T00:00:00",
        "query": "search term",
        "k": 3,
        "hop_budget": 1,
        "max_nodes": 1,
        "rank_count": 5,
        "result_count": 3,
    })
    lines = (tmp_path / "query_log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    parsed = json.loads(lines[0])
    for field in ("ts", "query", "k", "hop_budget", "max_nodes", "rank_count", "result_count"):
        assert field in parsed
