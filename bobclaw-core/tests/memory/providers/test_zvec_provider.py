from __future__ import annotations

import ast
import asyncio
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.ledger.federation import FederationRegistry
from core.memory.acl import ACLRegistry
from core.memory.exceptions import RetrievalProviderError
from core.memory.indexer import MemoryIndexer
from core.memory.models import ChunkRecord, ConfidenceStub, Fact, SlotResolution
from core.memory.write_fence import WriteFence, WriteFenceViolation


@pytest.fixture
def tmp_path() -> Path:
    root = (
        Path(__file__).resolve().parents[4]
        / "_workspace"
        / "testing"
        / "zvec-provider-pytest-r1"
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir()
    return path


def _acl(tmp_path: Path) -> ACLRegistry:
    path = tmp_path / "stores.toml"
    path.write_text(
        "[store.s]\n"
        'allowed_locality = ["local"]\n'
        'allowed_provider_ids = ["zvec-local"]\n'
        'allowed_capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    return ACLRegistry(path)


def _provider(
    tmp_path: Path,
    *,
    write_fence=None,
    collection_prefix: str = "bobclaw_",
    reclaim_timeout_s: float = 10.0,
    request_timeout_s: float = 10.0,
    worker_command: list[str] | None = None,
):
    from core.memory.providers.zvec_provider import ZvecRetrievalProvider

    return ZvecRetrievalProvider(
        provider_id="zvec-local",
        locality="local",
        collection_prefix=collection_prefix,
        acl_registry=_acl(tmp_path),
        store_root=tmp_path / "zvec-root",
        write_fence=write_fence,
        reclaim_timeout_s=reclaim_timeout_s,
        request_timeout_s=request_timeout_s,
        worker_command=worker_command,
    )


def _worker_command(source: str) -> list[str]:
    return [sys.executable, "-u", "-c", textwrap.dedent(source)]


def _fence(tmp_path: Path, collection_prefix: str = "bobclaw_") -> WriteFence:
    return WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix=collection_prefix,
        lock_dir=tmp_path / "locks",
    )


def _item(
    chunk_id: str,
    vector: list[float],
    fact_id: str | None = "f1",
    **payload,
) -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        vector=vector,
        payload={
            "source_fact_id": fact_id,
            "text": payload.pop("text", chunk_id),
            **payload,
        },
    )


def _fact() -> Fact:
    return Fact(
        fact_id="f1",
        generation_method="test",
        body={"text": "reindexed fact"},
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture(autouse=True)
def _close_providers():
    providers = []
    yield providers
    for provider in providers:
        provider.close()


def test_ipc_live_silent_child_times_out_drains_stderr_and_is_killed(
    tmp_path: Path, _close_providers
):
    provider = _provider(
        tmp_path,
        request_timeout_s=0.35,
        worker_command=_worker_command(
            """
            import sys
            import time

            sys.stderr.write("x" * 2_000_000)
            sys.stderr.flush()
            sys.stdin.readline()
            time.sleep(30)
            """
        ),
    )
    _close_providers.append(provider)
    child = provider._child
    assert child is not None
    log_dir = tmp_path / "zvec-root" / "_ipc"
    ready_deadline = time.monotonic() + 3.0
    while time.monotonic() < ready_deadline:
        logs = list(log_dir.glob("*.stderr.log"))
        if logs and logs[0].stat().st_size >= 2_000_000:
            break
        time.sleep(0.025)
    else:
        pytest.fail("fake child did not become live and drain stderr")

    t0 = time.monotonic()
    with pytest.raises(RetrievalProviderError, match="timed out"):
        provider._request("ping", deadline=time.monotonic() + 0.35)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5
    assert child.poll() is not None
    assert provider._child is None
    logs = list((tmp_path / "zvec-root" / "_ipc").glob("*.stderr.log"))
    assert logs and logs[0].stat().st_size >= 2_000_000


@pytest.mark.parametrize(
    "frame",
    [
        "[]",
        "{}",
        '{"ok": true}',
        '{"ok": false}',
        '{"ok": false, "error": "bad"}',
        '{"ok": false, "error": {"type": "Broken"}}',
    ],
)
def test_malformed_child_responses_kill_child_and_fail_closed(
    tmp_path: Path, _close_providers, frame: str
):
    provider = _provider(
        tmp_path,
        request_timeout_s=5.0,
        worker_command=_worker_command(
            f"""
            import sys
            import time

            sys.stdin.readline()
            print({frame!r}, flush=True)
            time.sleep(30)
            """
        ),
    )
    _close_providers.append(provider)
    child = provider._child
    assert child is not None

    with pytest.raises(RetrievalProviderError, match="malformed.*response"):
        provider._request("ping", deadline=time.monotonic() + 5.0)

    assert child.poll() is not None
    assert provider._child is None


@pytest.mark.parametrize(
    "prefix",
    [
        "../escape",
        "bobclaw/escape",
        "bobclaw\\escape",
        "/absolute",
        r"C:\tmp\absolute-escape",
        "...",
        "C:" + "\t" + "mp" + "\a" + "bsolute-escape",
    ],
)
def test_collection_prefix_rejects_non_name_components(
    tmp_path: Path, prefix: str
):
    with pytest.raises(ValueError, match="collection_prefix"):
        _provider(tmp_path, collection_prefix=prefix)


def test_multidim_family_index_preflights_and_succeeds_under_held_fence(
    tmp_path: Path, _close_providers
):
    fence = _fence(tmp_path)
    provider = _provider(tmp_path, write_fence=fence)
    _close_providers.append(provider)
    try:
        receipt = provider.index(
            "s",
            [
                _item("chunk:f1:three", [1.0, 0.0, 0.0]),
                _item("chunk:f1:four", [0.0, 1.0, 0.0, 0.0]),
            ],
        )

        assert receipt.item_count == 2
        collections = tmp_path / "zvec-root" / "instances" / "s" / "collections"
        assert (collections / "bobclaw__3").is_dir()
        assert (collections / "bobclaw__4").is_dir()
        hits = provider.query_vector("s", [1.0, 0.0, 0.0], 2).hits
        assert [hit.payload["chunk_id"] for hit in hits] == ["chunk:f1:three"]
    finally:
        fence.close()


def test_multidim_lock_preflight_times_out_with_zero_mutation(
    tmp_path: Path, _close_providers
):
    seeder = _provider(tmp_path)
    _close_providers.append(seeder)
    seeder.index(
        "s",
        [
            _item("chunk:seed:three", [1.0, 0.0, 0.0], fact_id="seed"),
            _item("chunk:seed:four", [0.0, 1.0, 0.0, 0.0], fact_id="seed"),
        ],
    )
    seeder.close()

    locker = _provider(tmp_path)
    writer = _provider(tmp_path, reclaim_timeout_s=0.75)
    _close_providers.extend([locker, writer])
    assert locker.query_vector("s", [0.0, 1.0, 0.0, 0.0], 1).hits

    t0 = time.monotonic()
    with pytest.raises(RetrievalProviderError, match="reclaim timed out"):
        writer.index(
            "s",
            [
                _item("chunk:new:three", [1.0, 0.0, 0.0], fact_id="new"),
                _item("chunk:new:four", [0.0, 1.0, 0.0, 0.0], fact_id="new"),
            ],
        )
    elapsed = time.monotonic() - t0

    assert 0.5 <= elapsed <= 1.75
    assert writer._last_reclaim_retries >= 1
    assert list(locker.scroll_payload("s", {"source_fact_id": "new"})) == []


def test_index_child_preflight_closes_create_and_lock_race(
    tmp_path: Path, _close_providers
):
    seeder = _provider(tmp_path)
    _close_providers.append(seeder)
    seeder.index(
        "s",
        [_item("chunk:seed:three", [1.0, 0.0, 0.0], fact_id="seed")],
    )
    seeder.close()

    locker = _provider(tmp_path)
    writer = _provider(tmp_path, reclaim_timeout_s=0.75)
    _close_providers.extend([locker, writer])
    original_call = writer._call_with_reclaim
    raced = False

    def race_before_index(operation: str, **payload):
        nonlocal raced
        if operation == "index" and not raced:
            locker.index(
                "s",
                [_item("chunk:lock:four", [0.0, 1.0, 0.0, 0.0], fact_id="lock")],
            )
            assert locker.query_vector("s", [0.0, 1.0, 0.0, 0.0], 1).hits
            raced = True
        return original_call(operation, **payload)

    writer._call_with_reclaim = race_before_index
    with pytest.raises(RetrievalProviderError, match="reclaim timed out"):
        writer.index(
            "s",
            [
                _item("chunk:new:three", [1.0, 0.0, 0.0], fact_id="new"),
                _item("chunk:new:four", [0.0, 1.0, 0.0, 0.0], fact_id="new"),
            ],
        )

    assert raced is True
    writer.close()
    reader = _provider(tmp_path)
    _close_providers.append(reader)
    assert reader.query_vector(
        "s",
        [1.0, 0.0, 0.0],
        10,
        filters={"source_fact_id": "new"},
    ).hits == []

def test_out_of_family_fence_refuses_mutation_before_zvec_write(
    tmp_path: Path, _close_providers
):
    fence = _fence(tmp_path, collection_prefix="other_")
    provider = _provider(tmp_path, write_fence=fence)
    _close_providers.append(provider)
    try:
        with pytest.raises(WriteFenceViolation, match="outside protected family"):
            provider.index("s", [_item("chunk:f1:x", [1.0, 0.0, 0.0])])
        assert not (tmp_path / "zvec-root" / "instances" / "s").exists()
    finally:
        fence.close()


def test_write_batches_1100_docs_and_scrolls_bounded_pages(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    items = [
        _item(f"chunk:bulk:{index}", [1.0, 0.0, 0.0], fact_id="bulk")
        for index in range(1100)
    ]

    receipt = provider.index("s", items)
    ids = list(provider.scroll_payload("s", {"source_fact_id": "bulk"}, batch_size=128))

    assert receipt.item_count == 1100
    assert len(ids) == 1100
    assert provider._last_stream_page_sizes == [128] * 8 + [76]


def test_late_invalid_document_is_rejected_before_any_upsert(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    items = [
        _item(f"chunk:bulk:{index}", [1.0, 0.0, 0.0], fact_id="bulk")
        for index in range(1100)
    ]
    items[1024] = ChunkRecord(
        id="chunk:bulk:invalid",
        vector=[1.0, 0.0, 0.0],
        payload={"source_fact_id": 42, "text": "invalid"},
    )

    with pytest.raises(
        RetrievalProviderError,
        match=r"source_fact_id must be a string or None",
    ):
        provider.index("s", items)

    assert list(provider.scroll_payload("s", {"source_fact_id": "bulk"})) == []


def test_source_fact_id_backslashes_round_trip_in_equality_filters(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    source_fact_id = r"C:\tmp\absolute-escape"
    provider.index(
        "s",
        [_item("chunk:path:one", [1.0, 0.0, 0.0], fact_id=source_fact_id)],
    )

    hits = provider.query_vector(
        "s",
        [1.0, 0.0, 0.0],
        10,
        filters={"source_fact_id": source_fact_id},
    ).hits
    ids = list(provider.scroll_payload("s", {"source_fact_id": source_fact_id}))

    assert [hit.payload["source_fact_id"] for hit in hits] == [source_fact_id]
    assert len(ids) == 1


def test_unsupported_filters_fail_closed_with_supported_key_detail(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index("s", [_item("chunk:f1:one", [1.0, 0.0, 0.0])])

    with pytest.raises(RetrievalProviderError, match="supported.*source_fact_id"):
        provider.query_vector(
            "s",
            [1.0, 0.0, 0.0],
            1,
            filters={"source_path": "fact://f1"},
        )
    with pytest.raises(RetrievalProviderError, match="supported.*source_fact_id"):
        list(provider.scroll_payload("s", {"source_path": "fact://f1"}))


def _literal_filter_keys(node: ast.AST) -> set[str] | None:
    if isinstance(node, ast.Constant) and node.value is None:
        return set()
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        keys.add(key.value)
    return keys


def _filter_drift_violations(source: str) -> tuple[set[str], list[str]]:
    tree = ast.parse(source)
    retriever_aliases = {"retriever"}
    scroll_method_aliases: set[str] = set()
    search_method_aliases: set[str] = set()

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = [target.id for target in targets if isinstance(target, ast.Name)]
            if isinstance(value, ast.Name) and value.id in retriever_aliases:
                before = len(retriever_aliases)
                retriever_aliases.update(target_names)
                changed = changed or len(retriever_aliases) != before
            if isinstance(value, ast.Attribute) and value.attr == "scroll_payload":
                before = len(scroll_method_aliases)
                scroll_method_aliases.update(target_names)
                changed = changed or len(scroll_method_aliases) != before
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "search"
                and isinstance(value.value, ast.Name)
                and value.value.id in retriever_aliases
            ):
                before = len(search_method_aliases)
                search_method_aliases.update(target_names)
                changed = changed or len(search_method_aliases) != before

    observed_provider_keys: set[str] = set()
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_scroll = (
            isinstance(node.func, ast.Attribute) and node.func.attr == "scroll_payload"
        ) or (isinstance(node.func, ast.Name) and node.func.id in scroll_method_aliases)
        if is_scroll:
            keyword_filters = [
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "payload_filter"
            ]
            filter_node = keyword_filters[0] if keyword_filters else (
                node.args[1] if len(node.args) >= 2 else None
            )
            keys = _literal_filter_keys(filter_node) if filter_node is not None else None
            if keys is None:
                violations.append("dynamic scroll_payload filter")
            elif not keys <= {"source_fact_id"}:
                violations.append(f"unsupported scroll_payload keys: {sorted(keys)!r}")
            else:
                observed_provider_keys.update(keys)

        is_retriever_search = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "search"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in retriever_aliases
        ) or (isinstance(node.func, ast.Name) and node.func.id in search_method_aliases)
        if is_retriever_search:
            for keyword in node.keywords:
                if keyword.arg != "filters":
                    continue
                keys = _literal_filter_keys(keyword.value)
                if keys is None:
                    violations.append("dynamic retriever filters")
                elif not keys <= {"source_fact_id", "include_deprecated"}:
                    violations.append(f"unsupported retriever keys: {sorted(keys)!r}")
                else:
                    observed_provider_keys.update(keys - {"include_deprecated"})

    return observed_provider_keys, violations


@pytest.mark.parametrize(
    "source",
    [
        'provider.scroll_payload("s", payload_filter={"source_path": "x"})',
        'provider.scroll_payload("s", payload_filter=dynamic_filters)',
        'alias = retriever\nalias.search("q", filters={"source_path": "x"})',
    ],
)
def test_filter_drift_guard_rejects_auditor_blind_spots(source: str):
    _, violations = _filter_drift_violations(source)
    assert violations


def test_production_filter_call_sites_do_not_drift_beyond_supported_set():
    core_root = Path(__file__).resolve().parents[3] / "core"
    api_root = Path(__file__).resolve().parents[3] / "api"
    observed_provider_keys: set[str] = set()
    violations: list[str] = []

    for path in [*core_root.rglob("*.py"), *api_root.rglob("*.py")]:
        observed, file_violations = _filter_drift_violations(
            path.read_text(encoding="utf-8")
        )
        observed_provider_keys.update(observed)
        violations.extend(f"{path}: {violation}" for violation in file_violations)

    assert observed_provider_keys == {"source_fact_id"}
    assert violations == []


def test_canonical_source_fact_id_encoding_round_trips_all_three_states(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index(
        "s",
        [
            ChunkRecord(
                id="chunk:missing",
                vector=[1.0, 0.0, 0.0],
                payload={"text": "missing"},
            ),
            _item("chunk:none", [0.0, 1.0, 0.0], fact_id=None),
            _item("chunk:empty", [0.0, 0.0, 1.0], fact_id=""),
        ],
    )

    hits = provider.query_vector("s", [1.0, 0.0, 0.0], 10).hits
    payloads = {hit.payload["chunk_id"]: hit.payload for hit in hits}
    empty_hits = provider.query_vector(
        "s",
        [0.0, 0.0, 1.0],
        10,
        filters={"source_fact_id": ""},
    ).hits
    empty_ids = list(provider.scroll_payload("s", {"source_fact_id": ""}))

    assert "source_fact_id" not in payloads["chunk:missing"]
    assert payloads["chunk:none"]["source_fact_id"] is None
    assert payloads["chunk:empty"]["source_fact_id"] == ""
    assert [hit.payload["chunk_id"] for hit in empty_hits] == ["chunk:empty"]
    assert len(empty_ids) == 1


def test_delete_and_scroll_exclude_out_of_family_lookalike_directories(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index(
        "s",
        [
            _item("chunk:f1:three", [1.0, 0.0, 0.0]),
            _item("chunk:f1:four", [0.0, 1.0, 0.0, 0.0]),
        ],
    )

    collections = tmp_path / "zvec-root" / "instances" / "s" / "collections"
    for name in ("bobclaw__junk", "bobclaw___3", "other__3"):
        (collections / name).mkdir()

    point_ids = list(provider.scroll_payload("s", {"source_fact_id": "f1"}))
    assert len(point_ids) == 2
    provider.delete("s", point_ids)
    assert list(provider.scroll_payload("s", {"source_fact_id": "f1"})) == []


def test_reindex_and_fact_delete_cover_all_existing_family_dimensions(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index(
        "s",
        [
            _item("chunk:f1:three", [1.0, 0.0, 0.0]),
            _item("chunk:f1:four", [0.0, 1.0, 0.0, 0.0]),
        ],
    )
    fact_store = SimpleNamespace(get=AsyncMock(return_value=_fact()))
    embedder = SimpleNamespace(
        embed_doc=AsyncMock(return_value=[[0.0, 1.0, 0.0, 0.0]])
    )
    slots = SimpleNamespace(
        get=lambda _: SlotResolution(
            slot_name="embed_text",
            model="m",
            backend="b",
            endpoint="e",
            embedding_dimension=4,
        )
    )
    indexer = MemoryIndexer(
        fact_store=fact_store,
        embedder=embedder,
        provider=provider,
        store_id="s",
        slot_resolver=slots,
    )

    assert asyncio.run(indexer.drop_facts(["f1"])) == 2
    assert asyncio.run(indexer.reindex_facts(["f1"])).errors == []
    assert provider.query_vector("s", [1.0, 0.0, 0.0], 10).hits == []
    assert len(provider.query_vector("s", [0.0, 1.0, 0.0, 0.0], 10).hits) == 1


def test_none_fence_legacy_shape_still_indexes_and_uses_strict_directory_selection(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path, write_fence=None)
    _close_providers.append(provider)
    provider.index("s", [_item("chunk:f1:legacy", [1.0, 0.0, 0.0])])
    lookalike = (
        tmp_path
        / "zvec-root"
        / "instances"
        / "s"
        / "collections"
        / "bobclaw__junk"
    )
    lookalike.mkdir()

    ids = list(provider.scroll_payload("s", {"source_fact_id": "f1"}))
    provider.delete("s", ids)
    assert list(provider.scroll_payload("s", {"source_fact_id": "f1"})) == []


def test_child_crash_surfaces_clean_provider_error_then_restarted_child_serves_reads(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index("s", [_item("chunk:f1:crash", [1.0, 0.0, 0.0])])
    child = provider._child
    assert child is not None
    child.kill()
    child.wait(timeout=5)

    with pytest.raises(RetrievalProviderError, match="storage child"):
        provider.query_vector("s", [1.0, 0.0, 0.0], 1)

    recovered = provider.query_vector("s", [1.0, 0.0, 0.0], 1)
    assert [hit.payload["chunk_id"] for hit in recovered.hits] == ["chunk:f1:crash"]


def test_kill_reclaims_within_bounded_window_under_real_lock_contention(
    tmp_path: Path, _close_providers
):
    locker = _provider(tmp_path)
    reclaimer = _provider(tmp_path, reclaim_timeout_s=2.5)
    _close_providers.extend([locker, reclaimer])
    locker.index("s", [_item("chunk:f1:reclaim", [1.0, 0.0, 0.0])])
    child = locker._child
    assert child is not None

    def kill_holder() -> None:
        time.sleep(0.5)
        child.kill()
        child.wait(timeout=5)

    killer = threading.Thread(target=kill_holder)
    killer.start()
    t0 = time.monotonic()
    results = reclaimer.query_vector("s", [1.0, 0.0, 0.0], 1)
    elapsed = time.monotonic() - t0
    killer.join(timeout=5)

    assert 0.5 <= elapsed <= 3.0
    assert reclaimer._last_reclaim_retries >= 1
    assert len(results.hits) == 1


def test_health_recovers_after_lock_releases_and_clears_sticky_error(
    tmp_path: Path, _close_providers
):
    owner = _provider(tmp_path)
    contender = _provider(tmp_path, reclaim_timeout_s=0.5)
    _close_providers.extend([owner, contender])
    owner.index("s", [_item("chunk:f1:lock", [1.0, 0.0, 0.0])])

    with pytest.raises(RetrievalProviderError, match="reclaim timed out"):
        contender.query_vector("s", [1.0, 0.0, 0.0], 1)
    assert contender.health().ok is False

    owner.close()
    recovered = contender.health()

    assert recovered.ok is True
    assert recovered.detail == ""
    assert contender._last_error == ""


def test_provider_conforms_to_declared_retrieval_protocol(
    tmp_path: Path, _close_providers
):
    from core.memory.interfaces import RetrievalProvider

    provider = _provider(tmp_path)
    _close_providers.append(provider)
    assert isinstance(provider, RetrievalProvider)
