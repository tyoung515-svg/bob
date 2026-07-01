from __future__ import annotations

from pathlib import Path

import pytest

from core.memory.exceptions import SlotDeferred, SlotMisconfigured
from core.memory.slots import SlotResolver

_FIXTURES = Path(__file__).parent / "fixtures" / "slots"
_CONFIG = Path(__file__).parent.parent.parent.parent / "bobclaw-core" / "config" / "memory_slots.toml"


class TestSlotResolver:
    def test_loads_default_config(self):
        resolver = SlotResolver(_CONFIG)
        sr = resolver.get("embed_text")
        assert sr.model == "granite-embedding-311m"
        assert sr.backend == "lmstudio"
        assert sr.endpoint == "http://localhost:8081"
        assert sr.embedding_dimension == 768

    def test_deferred_slot_raises(self):
        resolver = SlotResolver(_CONFIG)
        with pytest.raises(SlotDeferred, match="synth_deep"):
            resolver.get("synth_deep")

    def test_get_unknown_slot(self):
        resolver = SlotResolver(_CONFIG)
        with pytest.raises(SlotMisconfigured, match="not declared"):
            resolver.get("nonexistent_slot")


class TestSlotResolverFixtures:
    def test_valid_minimal(self):
        resolver = SlotResolver(_FIXTURES / "valid_minimal.toml")
        sr = resolver.get("embed_text")
        assert sr.model == "test-embedder"
        assert sr.embedding_dimension == 128

    def test_valid_deferred_raises(self):
        resolver = SlotResolver(_FIXTURES / "valid_minimal.toml")
        with pytest.raises(SlotDeferred, match="synth_deep"):
            resolver.get("synth_deep")

    def test_missing_required_raises(self):
        with pytest.raises(SlotMisconfigured, match="missing required keys"):
            SlotResolver(_FIXTURES / "missing_required.toml")

    def test_unknown_slot_in_file_raises(self):
        with pytest.raises(SlotMisconfigured, match="unknown slot name"):
            SlotResolver(_FIXTURES / "unknown_slot.toml")

    def test_deferred_only_config(self):
        resolver = SlotResolver(_FIXTURES / "deferred_only.toml")
        assert resolver.all_active() == []

    def test_is_active(self):
        resolver = SlotResolver(_FIXTURES / "valid_minimal.toml")
        assert resolver.is_active("embed_text") is True
        assert resolver.is_active("synth_deep") is False
        assert resolver.is_active("nonexistent") is False
