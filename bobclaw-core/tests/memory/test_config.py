from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.memory.config import MemoryConfig
from core.memory.exceptions import MemoryConfigError


class TestMemoryConfigDefaults:
    def test_defaults_round_trip(self):
        cfg = MemoryConfig()
        assert cfg.enabled is True
        assert cfg.data_dir == Path("./bobclaw-core/data")
        assert cfg.slots_file == Path("./bobclaw-core/config/memory_slots.toml")
        assert cfg.qdrant_collection == "bobclaw_l1_text_dense"
        assert cfg.top_k == 3
        assert cfg.threshold == 0.35
        assert cfg.wiki_dir == Path("./bobclaw-core/data/memory_wiki")
        assert cfg.watch_wiki is True
        assert cfg.hop_budget == 1

    def test_frozen(self):
        cfg = MemoryConfig()
        with pytest.raises(AttributeError):
            cfg.enabled = False


class TestMemoryConfigValidation:
    def test_top_k_zero_raises(self):
        with pytest.raises(MemoryConfigError, match="top_k must be >= 1"):
            MemoryConfig(top_k=0)

    def test_top_k_negative_raises(self):
        with pytest.raises(MemoryConfigError, match="top_k must be >= 1"):
            MemoryConfig(top_k=-1)

    def test_threshold_zero_raises(self):
        with pytest.raises(MemoryConfigError, match="threshold must be between"):
            MemoryConfig(threshold=0.0)

    def test_threshold_one_raises(self):
        with pytest.raises(MemoryConfigError, match="threshold must be between"):
            MemoryConfig(threshold=1.0)

    def test_threshold_above_one_raises(self):
        with pytest.raises(MemoryConfigError, match="threshold must be between"):
            MemoryConfig(threshold=1.5)

    def test_hop_budget_zero_raises(self):
        with pytest.raises(MemoryConfigError, match="hop_budget must be >= 1"):
            MemoryConfig(hop_budget=0)

    def test_valid_values_pass(self):
        cfg = MemoryConfig(top_k=5, threshold=0.5, hop_budget=2)
        assert cfg.top_k == 5
        assert cfg.threshold == 0.5
        assert cfg.hop_budget == 2


class TestMemoryConfigFromEnv:
    def test_from_env_defaults(self):
        cfg = MemoryConfig.from_env()
        assert cfg.enabled is True
        assert cfg.top_k == 3

    def test_from_env_with_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MEMORY_ENABLED", "false")
        monkeypatch.setenv("MEMORY_TOP_K", "10")
        monkeypatch.setenv("MEMORY_THRESHOLD", "0.5")
        monkeypatch.setenv("MEMORY_HOP_BUDGET", "2")
        monkeypatch.setenv("MEMORY_DATA_DIR", "/tmp/memory_data")
        monkeypatch.setenv("MEMORY_QDRANT_COLLECTION", "custom_collection")

        cfg = MemoryConfig.from_env()
        assert cfg.enabled is False
        assert cfg.top_k == 10
        assert cfg.threshold == 0.5
        assert cfg.hop_budget == 2
        assert cfg.data_dir == Path("/tmp/memory_data")
        assert cfg.qdrant_collection == "custom_collection"

    def test_from_env_bad_top_k_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MEMORY_TOP_K", "0")
        with pytest.raises(MemoryConfigError):
            MemoryConfig.from_env()

    def test_from_env_bad_threshold_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MEMORY_THRESHOLD", "0.0")
        with pytest.raises(MemoryConfigError):
            MemoryConfig.from_env()
