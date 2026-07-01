from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    from core.memory.interfaces import Indexer

log = logging.getLogger(__name__)


class _WikiEventHandler(FileSystemEventHandler):
    def __init__(self, q: queue.Queue) -> None:
        self._queue = q

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._queue.put(("created", event.src_path))

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._queue.put(("modified", event.src_path))

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._queue.put(("deleted", event.src_path))

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._queue.put(("deleted", event.src_path))
            self._queue.put(("created", event.dest_path))


class WikiWatcher:
    def __init__(self, wiki_dir: Path) -> None:
        self._wiki_dir = wiki_dir
        self._wiki_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue = queue.Queue()
        self._observer = Observer()
        self._event_handler = _WikiEventHandler(self._queue)
        self._sentinel = object()

    def start(self) -> None:
        self._observer.schedule(
            self._event_handler, str(self._wiki_dir), recursive=False
        )
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)
        self._queue.put(self._sentinel)

    async def drain(self, indexer: Indexer) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is self._sentinel:
                break

            if isinstance(item, tuple):
                event_type, path = item
                if event_type == "deleted":
                    log.debug("file deleted: %s", path)
                else:
                    log.debug("file changed: %s (%s)", path, event_type)
