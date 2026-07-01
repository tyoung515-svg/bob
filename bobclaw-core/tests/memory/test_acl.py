from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from core.memory.acl import ACLRegistry, StoreACL
from core.memory.exceptions import ACLViolation, MemoryConfigError


def _write_stores(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "stores.toml"
    p.write_text(text, encoding="utf-8")
    return p


class TestStoreACL:
    def test_loads_from_valid_toml(self, tmp_path):
        f = _write_stores(
            tmp_path,
            """
[store.test]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]
""",
        )
        reg = ACLRegistry(f)
        acl = reg.get("test")
        assert acl.store_id == "test"
        assert acl.allowed_locality == frozenset({"local"})
        assert acl.allowed_provider_ids == frozenset({"qdrant-local"})
        assert acl.allowed_capability_classes == frozenset({"text_dense"})

    def test_unknown_store_raises(self, tmp_path):
        f = _write_stores(tmp_path, "")
        reg = ACLRegistry(f)
        with pytest.raises(ACLViolation) as exc:
            reg.get("nobody")
        assert "unknown store" in str(exc.value)

    def test_enforce_allows_when_all_match(self, tmp_path):
        f = _write_stores(
            tmp_path,
            """
[store.ok]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]
""",
        )
        reg = ACLRegistry(f)
        reg.enforce("ok", "qdrant-local", "local", "text_dense")

    def test_enforce_denies_wrong_provider_id(self, tmp_path):
        f = _write_stores(
            tmp_path,
            """
[store.ok]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]
""",
        )
        reg = ACLRegistry(f)
        with pytest.raises(ACLViolation) as exc:
            reg.enforce("ok", "other-provider", "local", "text_dense")
        assert "provider_id" in str(exc.value)

    def test_enforce_denies_wrong_locality(self, tmp_path):
        f = _write_stores(
            tmp_path,
            """
[store.ok]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]
""",
        )
        reg = ACLRegistry(f)
        with pytest.raises(ACLViolation) as exc:
            reg.enforce("ok", "qdrant-local", "remote", "text_dense")
        assert "locality" in str(exc.value)

    def test_enforce_denies_wrong_capability_class(self, tmp_path):
        f = _write_stores(
            tmp_path,
            """
[store.ok]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]
""",
        )
        reg = ACLRegistry(f)
        with pytest.raises(ACLViolation) as exc:
            reg.enforce("ok", "qdrant-local", "local", "text_sparse")
        assert "capability_class" in str(exc.value)

    def test_enforce_logs_at_warn_with_store_id_and_reason_no_content(
        self, tmp_path
    ):
        f = _write_stores(
            tmp_path,
            """
[store.sec]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]
""",
        )
        reg = ACLRegistry(f)
        logger = logging.getLogger("bobclaw.memory.security")
        with (
            pytest.raises(ACLViolation),
            patch.object(logger, "warning") as mock_warn,
        ):
            reg.enforce("sec", "other-provider", "remote", "text_sparse")

        mock_warn.assert_called_once()
        args, _ = mock_warn.call_args
        msg = args[0] % args[1:] if len(args) > 1 else args[0]
        assert "sec" in msg
        assert "reason" in msg or "denied" or "violation" in msg
        assert "VECTOR" not in msg.upper()
        assert "PAYLOAD" not in msg.upper()

    def test_malformed_toml_missing_keys_raises(self, tmp_path):
        f = _write_stores(
            tmp_path,
            """
[store.bad]
allowed_locality = ["local"]
""",
        )
        with pytest.raises(MemoryConfigError):
            ACLRegistry(f)

    def test_store_acl_is_frozen(self):
        acl = StoreACL(
            store_id="x",
            allowed_locality=frozenset({"local"}),
            allowed_provider_ids=frozenset({"p"}),
            allowed_capability_classes=frozenset({"text_dense"}),
        )
        with pytest.raises(AttributeError):
            acl.store_id = "y"
