from __future__ import annotations

import asyncio
import queue
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.memory.watcher import WikiWatcher, _WikiEventHandler


class TestStartStop:
    def test_starts_and_stops_cleanly(self, tmp_path: Path):
        d = tmp_path / "wiki"
        d.mkdir(parents=True, exist_ok=True)
        w = WikiWatcher(d)
        w.start()
        time.sleep(0.3)
        w.stop()

    def test_double_stop_does_not_error(self, tmp_path: Path):
        d = tmp_path / "wiki"
        d.mkdir(parents=True, exist_ok=True)
        w = WikiWatcher(d)
        w.start()
        time.sleep(0.1)
        w.stop()
        w.stop()


class TestEventHandler:
    def test_file_create_lands_on_queue(self):
        handler = _WikiEventHandler(queue.Queue())
        handler.on_created(MagicMock(is_directory=False, src_path="/wiki/test.md"))
        assert handler._queue.qsize() == 1

    def test_file_modify_lands_on_queue(self):
        handler = _WikiEventHandler(queue.Queue())
        handler.on_modified(MagicMock(is_directory=False, src_path="/wiki/test.md"))
        assert handler._queue.qsize() == 1

    def test_file_delete_lands_on_queue(self):
        handler = _WikiEventHandler(queue.Queue())
        handler.on_deleted(MagicMock(is_directory=False, src_path="/wiki/test.md"))
        assert handler._queue.qsize() == 1

    def test_file_move_emits_two_events(self):
        handler = _WikiEventHandler(queue.Queue())
        handler.on_moved(
            MagicMock(is_directory=False, src_path="/wiki/old.md", dest_path="/wiki/new.md")
        )
        assert handler._queue.qsize() == 2


@pytest.mark.asyncio
async def test_drain_returns_on_stop():
    w = WikiWatcher(Path("."))
    w._queue.put(("created", "/wiki/test.md"))
    w._queue.put(w._sentinel)
    indexer = MagicMock()
    await w.drain(indexer)

