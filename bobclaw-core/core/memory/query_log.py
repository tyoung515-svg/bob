from __future__ import annotations

import json
import threading
from pathlib import Path


class QueryLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
