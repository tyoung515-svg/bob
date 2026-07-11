from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.ledger.federation import FederationError, FederationRegistry
from core.memory.acl import ACLRegistry
from core.memory.exceptions import MemoryConfigError
from core.memory.fingerprint import EmbedFingerprint
from core.memory.indexer import MemoryIndexer
from core.memory.lks_adapter import InstanceACL
from core.memory.models import ChunkRecord, ConfidenceStub, Fact, SlotResolution
from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
from core.memory.write_fence import (
    BOBCLAW_MEMORY_INSTANCE,
    WriteFence,
    WriteFenceViolation,
    backfill_corpus_acl,
    canonicalize_qdrant_url,
    enforce_write_acl,
    is_collection_in_family,
    register_bobclaw_memory,
)


DIM = 768


def _permissive_acl(tmp_path: Path) -> ACLRegistry:
    path = tmp_path / "stores.toml"
    path.write_text(
        "[store.s]\n"
        'allowed_locality = ["local"]\n'
        'allowed_provider_ids = ["p"]\n'
        'allowed_capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    return ACLRegistry(path)


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOBCLAW_WRITE_FENCE_LOCK_DIR", str(tmp_path / "locks"))


def _provider(tmp_path: Path, client, fence: WriteFence) -> QdrantRetrievalProvider:
    return QdrantRetrievalProvider(
        provider_id="p",
        locality="local",
        collection_prefix="bobclaw_",
        acl_registry=_permissive_acl(tmp_path),
        client=client,
        write_fence=fence,
    )


def _fact() -> Fact:
    return Fact(
        fact_id="f1",
        generation_method="test",
        body={"text": "family migration"},
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )


def test_bob_registration_keeps_exact_collection_uniqueness_without_self_acl(
    tmp_path: Path,
):
    registry = FederationRegistry(tmp_path / "registry.json")
    fingerprint = EmbedFingerprint("model", 768, True, "cosine")
    record = register_bobclaw_memory(registry, fingerprint)

    assert record["collection"] == "bobclaw__768"
    assert record["meta"]["embed"] == fingerprint.to_dict()
    assert "acl" not in record["meta"]
    with pytest.raises(FederationError):
        registry.register("external", "C:/external", collection="bobclaw__768", dim=768)


def test_external_corpus_acl_helpers_remain_separate_from_bob_family_writes(
    tmp_path: Path,
):
    registry = FederationRegistry(tmp_path / "registry.json")
    registry.register(
        "wiki",
        "C:/wiki",
        collection="wiki_chunks",
        dim=768,
        meta={"note": "retain"},
    )
    backfill_corpus_acl(registry, ["wiki"])
    metadata = registry.get("wiki")["meta"]
    assert metadata["note"] == "retain"
    assert metadata["acl"] == {
        "writer": "lks",
        "readers": ["bobclaw", "lks"],
        "mode": "ro",
    }

    writable_external_acl = InstanceACL("lks", frozenset({"lks"}), "rw")
    assert enforce_write_acl(writable_external_acl, "lks") is None
    with pytest.raises(WriteFenceViolation):
        enforce_write_acl(writable_external_acl, "bobclaw")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6353",
        "http://127.0.0.1:6353",
        "http://127.99.4.2:6353",
        "http://[::1]:6353",
    ],
)
def test_loopback_aliases_execute_as_one_held_family_lock(tmp_path: Path, url: str):
    registry = FederationRegistry(tmp_path / "registry.json")
    holder = WriteFence(
        registry,
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    contender = WriteFence(registry, qdrant_url=url, collection_prefix="bobclaw_")
    try:
        assert canonicalize_qdrant_url(url) == "http://localhost:6353"
        assert contender.lock_path == holder.lock_path
        assert contender.degraded is True
        assert contender.degraded_reason == "contention"
    finally:
        contender.close()
        holder.close()


def test_non_loopback_hosts_remain_distinct_without_dns(tmp_path: Path):
    registry = FederationRegistry(tmp_path / "registry.json")
    first = WriteFence(
        registry,
        qdrant_url="http://qdrant-a.example:6353",
        collection_prefix="bobclaw_",
    )
    second = WriteFence(
        registry,
        qdrant_url="http://qdrant-b.example:6353",
        collection_prefix="bobclaw_",
    )
    try:
        assert first.lock_path != second.lock_path
        assert second.degraded is False
    finally:
        second.close()
        first.close()


def test_strict_family_predicate_uses_ascii_positive_decimal_only():
    assert is_collection_in_family("bobclaw__1", "bobclaw_")
    assert is_collection_in_family("bobclaw__2560", "bobclaw_")
    for collection in (
        "bobclaw__0",
        "bobclaw__junk",
        "bobclaw___768",
        "bobclaw__١",
        "other__768",
    ):
        assert not is_collection_in_family(collection, "bobclaw_")


def test_legacy_collection_constructor_derives_family_and_has_no_collection_property(
    tmp_path: Path,
):
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection="bobclaw__768",
    )
    try:
        assert fence.collection_prefix == "bobclaw_"
        assert not hasattr(fence, "collection")
        assert fence.assert_writable("bobclaw__2560") is None
    finally:
        fence.close()


def test_legacy_collection_constructor_refuses_non_dim_shape(tmp_path: Path):
    with pytest.raises(WriteFenceViolation, match="positive ASCII decimal dimension"):
        WriteFence(
            FederationRegistry(tmp_path / "registry.json"),
            qdrant_url="http://localhost:6353",
            collection="shared_collection",
        )


def test_family_fence_authorizes_all_dimensions_without_self_acl(tmp_path: Path):
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    try:
        assert fence.assert_writable("bobclaw__1024") is None
        assert fence.assert_writable("bobclaw__2560") is None
        with pytest.raises(WriteFenceViolation, match="family"):
            fence.assert_writable("bobclaw__junk")
    finally:
        fence.close()


def test_foreign_registry_family_collision_refuses_fence_construction(tmp_path: Path):
    registry = FederationRegistry(tmp_path / "registry.json")
    registry.register(
        "external-lks",
        "C:/external",
        collection="bobclaw__768",
        dim=768,
    )

    with pytest.raises(WriteFenceViolation, match="registry collision"):
        WriteFence(
            registry,
            qdrant_url="http://localhost:6353",
            collection_prefix="bobclaw_",
        )


def test_permission_failure_has_distinct_degraded_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def refuse_acquire(self):
        raise PermissionError(errno.EACCES, "access denied")

    monkeypatch.setattr("core.memory.write_fence.FileLock.acquire", refuse_acquire)
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    try:
        assert fence.degraded is True
        assert fence.degraded_reason == "permission"
        with pytest.raises(WriteFenceViolation, match="permission"):
            fence.assert_writable("bobclaw__768")
    finally:
        fence.close()


def test_memory_enabled_rejects_explicit_fence_opt_out(monkeypatch: pytest.MonkeyPatch):
    from core.memory.bootstrap import _maybe_build_write_fence

    monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "false")
    with pytest.raises(MemoryConfigError, match="MEMORY_ENABLED.*MEMORY_WRITE_FENCE_ENABLED"):
        _maybe_build_write_fence(MagicMock(), "bobclaw_")


def test_provider_multidim_family_index_preflights_and_succeeds(tmp_path: Path):
    client = MagicMock()
    client.get_collection.return_value = MagicMock()
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    provider = _provider(tmp_path, client, fence)
    try:
        receipt = provider.index(
            "s",
            [
                ChunkRecord(id="old", vector=[0.1] * 1024, payload={}),
                ChunkRecord(id="new", vector=[0.2] * 2560, payload={}),
            ],
        )
        assert receipt.item_count == 2
        assert {
            call.kwargs["collection_name"] for call in client.upsert.call_args_list
        } == {"bobclaw__1024", "bobclaw__2560"}
    finally:
        fence.close()


def test_provider_delete_and_scroll_exclude_foreign_lookalikes(tmp_path: Path):
    client = MagicMock()
    client.get_collections.return_value = SimpleNamespace(
        collections=[
            SimpleNamespace(name="bobclaw__1024"),
            SimpleNamespace(name="bobclaw__2560"),
            SimpleNamespace(name="bobclaw__junk"),
            SimpleNamespace(name="bobclaw___768"),
            SimpleNamespace(name="other__768"),
        ]
    )
    client.scroll.return_value = ([], None)
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    provider = _provider(tmp_path, client, fence)
    try:
        provider.delete("s", ["id1"])
        assert {
            call.kwargs["collection_name"] for call in client.delete.call_args_list
        } == {"bobclaw__1024", "bobclaw__2560"}

        assert list(provider.scroll_payload("s", {"source_fact_id": "f1"})) == []
        assert {
            call.kwargs["collection_name"] for call in client.scroll.call_args_list
        } == {"bobclaw__1024", "bobclaw__2560"}
    finally:
        fence.close()


def test_reindex_and_fact_delete_cover_the_held_multidim_family_lock(tmp_path: Path):
    client = MagicMock()
    client.get_collections.return_value = SimpleNamespace(
        collections=[
            SimpleNamespace(name="bobclaw__1024"),
            SimpleNamespace(name="bobclaw__2560"),
            SimpleNamespace(name="bobclaw__junk"),
            SimpleNamespace(name="bobclaw___768"),
        ]
    )
    client.get_collection.return_value = MagicMock()
    client.scroll.return_value = ([SimpleNamespace(id="chunk:f1:old")], None)
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    provider = _provider(tmp_path, client, fence)
    fact_store = MagicMock()
    fact_store.get = AsyncMock(return_value=_fact())
    embedder = SimpleNamespace(embed=AsyncMock(return_value=[[0.1] * 2560]))
    slots = SimpleNamespace(
        get=lambda _: SlotResolution(
            slot_name="embed_text",
            model="m",
            backend="b",
            endpoint="e",
            embedding_dimension=2560,
        )
    )
    indexer = MemoryIndexer(
        fact_store=fact_store,
        embedder=embedder,
        provider=provider,
        store_id="s",
        slot_resolver=slots,
    )
    try:
        assert asyncio.run(indexer.drop_facts(["f1"])) == 2
        stats = asyncio.run(indexer.reindex_facts(["f1"]))
        assert stats.errors == []
        assert {
            call.kwargs["collection_name"] for call in client.delete.call_args_list
        } == {"bobclaw__1024", "bobclaw__2560"}
        assert client.delete.call_count == 4
        assert client.upsert.call_args.kwargs["collection_name"] == "bobclaw__2560"
        assert "bobclaw__junk" not in str(client.delete.call_args_list)
        assert "bobclaw___768" not in str(client.delete.call_args_list)
        delete_positions = [
            index for index, call in enumerate(client.mock_calls) if call[0] == "delete"
        ]
        upsert_position = next(
            index for index, call in enumerate(client.mock_calls) if call[0] == "upsert"
        )
        assert max(delete_positions) < upsert_position
    finally:
        fence.close()


def test_provider_without_write_fence_keeps_index_and_uses_strict_selection(
    tmp_path: Path,
):
    client = MagicMock()
    client.get_collection.return_value = MagicMock()
    client.get_collections.return_value = SimpleNamespace(
        collections=[
            SimpleNamespace(name="bobclaw__768"),
            SimpleNamespace(name="bobclaw__junk"),
            SimpleNamespace(name="bobclaw___768"),
        ]
    )
    client.scroll.return_value = ([], None)
    provider = QdrantRetrievalProvider(
        provider_id="p",
        locality="local",
        collection_prefix="bobclaw_",
        acl_registry=_permissive_acl(tmp_path),
        client=client,
    )

    receipt = provider.index(
        "s", [ChunkRecord(id="legacy", vector=[0.1] * 768, payload={})]
    )
    provider.delete("s", ["legacy"])
    assert list(provider.scroll_payload("s", {"source_fact_id": "f1"})) == []

    assert receipt.item_count == 1
    assert client.upsert.call_args.kwargs["collection_name"] == "bobclaw__768"
    assert {
        call.kwargs["collection_name"] for call in client.delete.call_args_list
    } == {"bobclaw__768"}
    assert {
        call.kwargs["collection_name"] for call in client.scroll.call_args_list
    } == {"bobclaw__768"}


def test_lock_directory_failure_is_loud_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bad_lock_path = tmp_path / "lock-file"
    bad_lock_path.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("BOBCLAW_WRITE_FENCE_LOCK_DIR", str(bad_lock_path))

    with pytest.raises(WriteFenceViolation, match="write-lock"):
        WriteFence(
            FederationRegistry(tmp_path / "registry.json"),
            qdrant_url="http://localhost:6353",
            collection_prefix="bobclaw_",
        )


def test_closed_fence_refuses_writes(tmp_path: Path):
    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    fence.close()

    with pytest.raises(WriteFenceViolation, match="not held"):
        fence.assert_writable("bobclaw__768")


def test_process_exit_releases_held_family_lock(tmp_path: Path):
    root = tmp_path / "process-exit"
    root.mkdir()
    env = _subprocess_env(root / "locks")
    first = subprocess.run(
        _process_args(
            "http://localhost:6353",
            root / "first-registry.json",
            root / "first.outcome",
            root / "first.ready",
            root / "unused-release",
            "exit",
        ),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "ACQUIRED"

    successor = subprocess.run(
        _process_args(
            "http://127.0.0.1:6353",
            root / "successor-registry.json",
            root / "successor.outcome",
            root / "successor.ready",
            root / "unused-release",
            "exit",
        ),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert successor.returncode == 0, successor.stderr
    assert successor.stdout.strip() == "ACQUIRED"


_PROCESS_FENCE_SCRIPT = textwrap.dedent(
    r"""
    import sys
    import time
    from pathlib import Path

    from core.ledger.federation import FederationRegistry
    from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
    from core.memory.write_fence import WriteFence, WriteFenceViolation

    class _ACL:
        def enforce(self, *args):
            return None

    class _Client:
        def collection_exists(self, collection):
            return True

        def query_points(self, **kwargs):
            return type("Result", (), {"points": []})()

    endpoint, registry_path, outcome_path, ready_path, release_path, role = sys.argv[1:]
    registry = FederationRegistry(Path(registry_path)).load()
    fence = WriteFence(registry, qdrant_url=endpoint, collection_prefix="bobclaw_")
    if fence.degraded:
        try:
            fence.assert_writable("bobclaw__768")
        except WriteFenceViolation:
            reader = QdrantRetrievalProvider(
                provider_id="p",
                locality="local",
                collection_prefix="bobclaw_",
                acl_registry=_ACL(),
                client=_Client(),
                write_fence=fence,
            )
            reader.query_vector("s", [0.1] * 768)
            Path(outcome_path).write_text(f"refused:{fence.degraded_reason}\\n", encoding="utf-8")
            print("REFUSED_READ_OK", flush=True)
            raise SystemExit(0)
        raise SystemExit("degraded fence unexpectedly permitted a write")

    Path(outcome_path).write_text("acquired\\n", encoding="utf-8")
    Path(ready_path).write_text("ready\\n", encoding="utf-8")
    print("ACQUIRED", flush=True)
    if role == "holder":
        deadline = time.monotonic() + 10
        while not Path(release_path).exists():
            if time.monotonic() >= deadline:
                raise SystemExit("holder timed out")
            time.sleep(0.01)
    """
).strip()


def _subprocess_env(lock_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (repo_root, env.get("PYTHONPATH")) if part
    )
    env["BOBCLAW_WRITE_FENCE_LOCK_DIR"] = str(lock_dir)
    return env


def _process_args(
    endpoint: str,
    registry_path: Path,
    outcome_path: Path,
    ready_path: Path,
    release_path: Path,
    role: str,
) -> list[str]:
    return [
        sys.executable,
        "-c",
        _PROCESS_FENCE_SCRIPT,
        endpoint,
        str(registry_path),
        str(outcome_path),
        str(ready_path),
        str(release_path),
        role,
    ]


def test_two_process_same_service_aliases_contend_then_take_over(tmp_path: Path):
    """Two install roots share one lock despite loopback URL spelling changes."""
    root = tmp_path / "two-process"
    root.mkdir()
    holder_root = root / "holder-install"
    contender_root = root / "contender-install"
    successor_root = root / "successor-install"
    for install_root in (holder_root, contender_root, successor_root):
        install_root.mkdir()

    release_path = root / "release"
    holder_outcome = root / "holder.outcome"
    holder_ready = root / "holder.ready"
    contender_outcome = root / "contender.outcome"
    env = _subprocess_env(root / "locks")

    holder = subprocess.Popen(
        _process_args(
            "http://localhost:6353",
            holder_root / "registry.json",
            holder_outcome,
            holder_ready,
            release_path,
            "holder",
        ),
        cwd=holder_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ACQUIRED"
        assert holder_ready.read_text(encoding="utf-8") == "ready\\n"

        contender = subprocess.run(
            _process_args(
                "http://127.0.0.1:6353",
                contender_root / "registry.json",
                contender_outcome,
                root / "contender.ready",
                release_path,
                "contender",
            ),
            cwd=contender_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.strip() == "REFUSED_READ_OK"
        assert contender_outcome.read_text(encoding="utf-8") == "refused:contention\\n"
    finally:
        release_path.write_text("release\\n", encoding="utf-8")
        _, holder_stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, holder_stderr

    successor = subprocess.run(
        _process_args(
            "http://[::1]:6353",
            successor_root / "registry.json",
            root / "successor.outcome",
            root / "successor.ready",
            release_path,
            "successor",
        ),
        cwd=successor_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert successor.returncode == 0, successor.stderr
    assert successor.stdout.strip() == "ACQUIRED"

def test_new_programdata_lock_directory_receives_explicit_users_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if os.name != "nt":
        pytest.skip("Windows ProgramData ACL contract")
    from core.memory.write_fence import _prepare_lock_dir

    program_data = tmp_path / "ProgramData"
    lock_dir = program_data / "bobclaw" / "locks"
    monkeypatch.setenv("ProgramData", str(program_data))
    calls = []

    def fake_icacls(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("core.memory.write_fence.subprocess.run", fake_icacls)
    assert _prepare_lock_dir(lock_dir) == lock_dir
    assert calls
    command = calls[0][0]
    assert "/inheritance:r" in command
    assert "*S-1-5-32-545:(OI)(CI)M" in command


def _assert_reserved_name_spoof_refused_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    collection: str,
    dim: int,
    repo: str = "C:/foreign",
):
    from core.memory.bootstrap import _maybe_build_write_fence

    registry_path = tmp_path / "registry.json"
    registry_type = FederationRegistry
    registry = registry_type(registry_path)
    original = registry.register(
        BOBCLAW_MEMORY_INSTANCE,
        repo,
        collection=collection,
        dim=dim,
        meta={"owner": "external"},
    )
    registry.save()
    loaded_registry = registry_type(registry_path).load()
    monkeypatch.setattr(
        "core.ledger.federation.FederationRegistry",
        lambda _: loaded_registry,
    )
    slot = SimpleNamespace(
        get=lambda _: SlotResolution(
            slot_name="embed_text",
            model="m",
            backend="b",
            endpoint="e",
            embedding_dimension=768,
        )
    )
    monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(registry_path))
    monkeypatch.delenv("MEMORY_WRITE_FENCE_ENABLED", raising=False)

    with pytest.raises(WriteFenceViolation, match="reserved registry name"):
        _maybe_build_write_fence(slot, "bobclaw_", "http://localhost:6353")

    assert loaded_registry.get(BOBCLAW_MEMORY_INSTANCE) == original
    assert registry_type(registry_path).load().get(BOBCLAW_MEMORY_INSTANCE) == original


def test_bootstrap_refuses_family_shaped_reserved_name_foreign_repo_spoof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Auditor premise: reserved name + family collection + foreign repo is refused intact."""
    _assert_reserved_name_spoof_refused_without_overwrite(
        tmp_path,
        monkeypatch,
        collection="bobclaw__1024",
        dim=1024,
    )


def test_bootstrap_refuses_out_of_family_reserved_name_spoof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The round-1 out-of-family spoof remains covered as a separate premise."""
    _assert_reserved_name_spoof_refused_without_overwrite(
        tmp_path,
        monkeypatch,
        collection="foreign_vectors",
        dim=768,
    )


def test_bootstrap_refuses_out_of_family_reserved_name_with_bob_repo_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Out-of-family ownership is refused even when the reserved repo signature is valid."""
    _assert_reserved_name_spoof_refused_without_overwrite(
        tmp_path,
        monkeypatch,
        collection="foreign_vectors",
        dim=768,
        repo=".",
    )


def test_bootstrap_rejects_foreign_historical_family_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from core.memory.bootstrap import _maybe_build_write_fence

    registry_path = tmp_path / "registry.json"
    registry = FederationRegistry(registry_path)
    registry.register(
        "external-lks",
        "C:/external",
        collection="bobclaw__1024",
        dim=1024,
    )
    registry.save()
    slot = SimpleNamespace(
        get=lambda _: SlotResolution(
            slot_name="embed_text",
            model="m",
            backend="b",
            endpoint="e",
            embedding_dimension=768,
        )
    )
    monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(registry_path))
    monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "true")
    with pytest.raises(WriteFenceViolation, match="registry collision"):
        _maybe_build_write_fence(slot, "bobclaw_", "http://localhost:6353")
