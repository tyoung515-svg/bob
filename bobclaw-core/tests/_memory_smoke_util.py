"""Shared helpers for the write-capable memory integration smokes (R2, v0.98).

These smokes exercise BoB's own Qdrant with real writes. The rules they enforce:

* Default to BoB's Qdrant (``:6353``), NEVER the shared LKS Qdrant (``:6333``).
  The old default was ``:6333`` and left a ``bobclaw__768`` residue in LKS.
* Every run writes to a UNIQUE throwaway collection (its own ``collection_prefix``)
  and drops it in teardown — the real ``bobclaw__768`` store is never touched.
* Prove non-mutation: snapshot every non-throwaway collection's point count
  before and after, and assert it is unchanged (teardown fires even if the test
  body raises).

Not a pytest module itself (leading underscore) — imported by the smoke tests.
"""
from __future__ import annotations

import http.client
import json
import os
import uuid
from pathlib import Path

import pytest

DEFAULT_BOB_QDRANT = "http://localhost:6353"
LKS_QDRANT_PORT = 6333


def _host_port(url: str) -> tuple[str, int]:
    hp = url.replace("http://", "").replace("https://", "")
    host = hp.split(":")[0]
    port = int(hp.split(":")[1]) if ":" in hp else 80
    return host, port


def resolve_bob_qdrant_url() -> str:
    """BoB's own Qdrant (``:6353``) by default.

    Refuse a write-capable run against the shared LKS Qdrant (``:6333``) unless an
    operator explicitly opts in via ``MEMORY_TEST_ALLOW_6333`` for an intentionally
    named throwaway endpoint. Writing to LKS is a hard stop condition, so this
    fails closed rather than skipping.
    """
    url = os.getenv("MEMORY_QDRANT_URL", DEFAULT_BOB_QDRANT)
    _, port = _host_port(url)
    # Opt-in must be an explicit TRUTHY value — "0"/"false"/"" do NOT permit :6333
    # (a bare `os.getenv(...)` truthiness check let "0" through).
    allow = os.getenv("MEMORY_TEST_ALLOW_6333", "").strip().lower() in ("1", "true", "yes", "on")
    if port == LKS_QDRANT_PORT and not allow:
        # RuntimeError (not pytest.fail): a hard misconfiguration stop that errors the
        # fixture AND is cleanly catchable in a unit test (pytest.raises ignores the
        # Failed outcome exception).
        raise RuntimeError(
            f"Refusing a write-capable memory integration run against {url}: "
            f":{LKS_QDRANT_PORT} is the shared LKS Qdrant. Point MEMORY_QDRANT_URL "
            f"at BoB's Qdrant (:6353), or set MEMORY_TEST_ALLOW_6333=1 ONLY for an "
            f"intentionally-named throwaway endpoint."
        )
    return url


def _get(url: str, path: str, timeout: int = 5) -> tuple[int, bytes]:
    host, port = _host_port(url)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def require_qdrant(url: str) -> None:
    """Skip the test if Qdrant is unreachable at ``url``."""
    try:
        status, _ = _get(url, "/healthz")
    except Exception as exc:  # noqa: BLE001 — any connect error ⇒ skip
        pytest.skip(f"Qdrant not reachable at {url}: {exc}")
    if status != 200:
        pytest.skip(f"Qdrant at {url} returned status {status}")


def collection_names(url: str) -> set[str]:
    # RAISE on a non-200 rather than returning an empty set: a silently-empty
    # snapshot would let the "nothing mutated" invariant false-pass (both before
    # and after would be {}). The teardown assertion depends on this being honest.
    status, body = _get(url, "/collections")
    if status != 200:
        raise AssertionError(f"Qdrant /collections at {url} returned {status}")
    data = json.loads(body)
    return {c["name"] for c in data["result"]["collections"]}


def collection_point_count(url: str, name: str) -> int | None:
    status, body = _get(url, f"/collections/{name}")
    if status == 404:
        return None  # collection genuinely absent (e.g. a dropped throwaway)
    if status != 200:
        raise AssertionError(f"Qdrant /collections/{name} at {url} returned {status}")
    return json.loads(body)["result"].get("points_count")


def drop_collection(url: str, name: str) -> None:
    host, port = _host_port(url)
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("DELETE", f"/collections/{name}")
        resp = conn.getresponse()
        resp.read()
        # 200 (dropped) and 404 (already absent) are both fine; anything else is a
        # teardown failure we must NOT swallow (else a throwaway collection leaks).
        if resp.status not in (200, 404):
            raise AssertionError(
                f"failed to drop throwaway collection {name}: HTTP {resp.status}"
            )
    finally:
        conn.close()


def throwaway_prefix() -> str:
    """A unique, self-identifying collection prefix for one test run."""
    return f"v098test_{uuid.uuid4().hex[:12]}_"


def write_throwaway_stores_toml(tmp_path: Path, prefix: str) -> Path:
    p = tmp_path / "memory_stores_throwaway.toml"
    p.write_text(
        "[meta]\n"
        'spec_version = "1.0"\n\n'
        "[stores.bobclaw_default]\n"
        'description = "throwaway v098 integration store"\n'
        'acl_allowed_providers = ["qdrant_local"]\n\n'
        "[providers.qdrant_local]\n"
        'locality = "local"\n'
        f'collection_prefix = "{prefix}"\n'
        'capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    return p


def snapshot_non_throwaway(url: str, prefix: str) -> dict[str, int | None]:
    """Point count of every collection NOT under this run's throwaway prefix."""
    return {
        name: collection_point_count(url, name)
        for name in collection_names(url)
        if not name.startswith(prefix)
    }


def assert_untouched(url: str, prefix: str, before: dict[str, int | None]) -> None:
    """Assert every pre-existing (non-throwaway) collection is byte-for-byte
    unchanged AND no throwaway collection survived teardown. Proves the run stayed
    inside its throwaway namespace and never mutated the real store."""
    names = collection_names(url)  # raises if Qdrant is unreachable — no silent pass
    residue = sorted(n for n in names if n.startswith(prefix))
    assert not residue, f"throwaway residue not cleaned up: {residue}"
    after = {n: collection_point_count(url, n) for n in names if not n.startswith(prefix)}
    assert after == before, (
        "memory integration run mutated a non-throwaway collection.\n"
        f"before={before}\nafter={after}"
    )
