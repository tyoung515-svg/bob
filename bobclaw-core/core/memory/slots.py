from __future__ import annotations

import tomllib
import os
import shlex
import stat
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
        self.keyword_fallback = tomllib.loads(slots_file.read_text(encoding="utf-8")).get("embedder_selection") is not None

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
    selection = None
    if "embedder_selection" in raw:
        # Runtime opt-in only. Do not let a slot config choose another env file.
        if raw["embedder_selection"] != "/etc/bob/embedder.env":
            raise SlotMisconfigured("embed_text", "invalid packaged embedder selection path")
        selection = _runtime_embedding_selection(Path(raw["embedder_selection"]))
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
        if selection and slot_name in {"embed_text", "embed_text_heavy"}:
            slots[slot_name].update(model=selection[0], embedding_dimension=selection[1])
    return slots


def _runtime_embedding_selection(path: Path) -> tuple[str, int]:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536
            or (os.name != "nt" and (info.st_uid != 0 or info.st_mode & 0o022))):
        raise SlotMisconfigured("embed_text", "unsafe packaged embedder selection")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith('#'):
            key, _, value = line.partition('=')
            tokens = shlex.split(value)
            if len(tokens) != 1:
                raise SlotMisconfigured("embed_text", "invalid packaged embedder selection")
            values[key] = tokens[0]
    choices = {"4b": ("qwen3-embedding-4b", 2560), "0.6b": ("qwen3-embedding-0.6b", 1024)}
    selected = choices.get(values.get("EMBEDDER_MODEL", "4b"))
    if selected is None or values.get("EMBEDDER_ALIAS", selected[0]) != selected[0] or values.get("EMBEDDER_DIM", str(selected[1])) != str(selected[1]):
        raise SlotMisconfigured("embed_text", "inconsistent packaged embedder model/dimension")
    return selected
