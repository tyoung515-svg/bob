from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core.memory.exceptions import MemoryConfigError


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    data_dir: Path = Path("./bobclaw-core/data")
    slots_file: Path = Path("./bobclaw-core/config/memory_slots.toml")
    qdrant_collection: str = "bobclaw_l1_text_dense"
    top_k: int = 3
    threshold: float = 0.35
    wiki_dir: Path = Path("./bobclaw-core/data/memory_wiki")
    watch_wiki: bool = True
    hop_budget: int = 1

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.top_k < 1:
            errors.append(f"top_k must be >= 1, got {self.top_k}")
        if not (0.0 < self.threshold < 1.0):
            errors.append(
                f"threshold must be between 0.0 and 1.0 (exclusive), got {self.threshold}"
            )
        if self.hop_budget < 1:
            errors.append(f"hop_budget must be >= 1, got {self.hop_budget}")
        if errors:
            raise MemoryConfigError("; ".join(errors))

    @classmethod
    def from_env(cls) -> MemoryConfig:
        return cls(
            enabled=os.getenv("MEMORY_ENABLED", "true").lower() == "true",
            data_dir=Path(os.getenv("MEMORY_DATA_DIR", "./bobclaw-core/data")),
            slots_file=Path(
                os.getenv("MEMORY_SLOTS_FILE", "./bobclaw-core/config/memory_slots.toml")
            ),
            qdrant_collection=os.getenv(
                "MEMORY_QDRANT_COLLECTION", "bobclaw_l1_text_dense"
            ),
            top_k=int(os.getenv("MEMORY_TOP_K", "3")),
            threshold=float(os.getenv("MEMORY_THRESHOLD", "0.35")),
            wiki_dir=Path(os.getenv("MEMORY_WIKI_DIR", "./bobclaw-core/data/memory_wiki")),
            watch_wiki=os.getenv("MEMORY_WATCH_WIKI", "true").lower() == "true",
            hop_budget=int(os.getenv("MEMORY_HOP_BUDGET", "1")),
        )
