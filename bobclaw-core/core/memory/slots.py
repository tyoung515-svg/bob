from __future__ import annotations

import tomllib
from pathlib import Path

from core.memory.exceptions import SlotDeferred, SlotMisconfigured
from core.memory.models import SlotResolution

_KNOWN_SLOTS: frozenset[str] = frozenset({
    "embed_text",
    "embed_text_heavy",
    "embed_multimodal",
    "embed_visual_doc",
    "extract_small",
    "route_cheap",
    "rollup_mid",
    "synth_mid",
    "synth_deep",
    "audit_manager",
    "rerank_cross",
    "managed_remote",
})


class SlotResolver:
    def __init__(self, slots_file: Path) -> None:
        self._file = slots_file
        self._slots: dict[str, dict] = _parse_slots_file(slots_file)

    def get(self, slot_name: str) -> SlotResolution:
        raw = self._slots.get(slot_name)
        if raw is None:
            raise SlotMisconfigured(slot_name, "not declared")
        if raw.get("deferred", False):
            raise SlotDeferred(slot_name)
        return SlotResolution(
            slot_name=slot_name,
            model=raw["model"],
            backend=raw["backend"],
            endpoint=raw["endpoint"],
            embedding_dimension=raw.get("embedding_dimension"),
        )

    def is_active(self, slot_name: str) -> bool:
        raw = self._slots.get(slot_name)
        if raw is None:
            return False
        return not raw.get("deferred", False)

    def all_active(self) -> list[str]:
        return [name for name, raw in self._slots.items() if not raw.get("deferred", False)]


def _parse_slots_file(path: Path) -> dict[str, dict]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    slot_section = raw.get("slot", {})
    slots: dict[str, dict] = {}
    for slot_name, value in slot_section.items():
        if slot_name not in _KNOWN_SLOTS:
            raise SlotMisconfigured(
                slot_name, f"unknown slot name {slot_name!r}; known: {sorted(_KNOWN_SLOTS)}"
            )
        if not value.get("deferred", False):
            missing = [k for k in ("model", "backend", "endpoint") if k not in value]
            if missing:
                raise SlotMisconfigured(
                    slot_name, f"missing required keys: {missing}"
                )
        slots[slot_name] = dict(value)
    return slots
