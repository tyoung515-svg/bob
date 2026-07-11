import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.memory.fingerprint import (
    EmbedFingerprint,
    FingerprintError,
    FingerprintMissing,
    FingerprintMismatch,
    fingerprint_from_slot,
    stamp_meta,
    read_meta_fingerprint,
    assert_compatible,
    assert_slot_matches_registry,
    sentinel_vector,
    write_sentinel,
    read_sentinel,
    assert_sentinel_matches,
    ensure_sentinel,
    SENTINEL_POINT_ID,
    SENTINEL_PAYLOAD_KEY,
    SENTINEL_MARKER_KEY,
)
from core.memory.models import SlotResolution
from core.ledger.federation import FederationRegistry


# ---------------------------------------------------------------------------
# 1. Dataclass round-trip, hashability, canonical distance
# ---------------------------------------------------------------------------
def test_dataclass_roundtrip_hashable_canonical():
    fp = EmbedFingerprint("granite-embedding-311m-r2", 768, True, "cosine")
    assert EmbedFingerprint.from_dict(fp.to_dict()) == fp
    assert fp == EmbedFingerprint("granite-embedding-311m-r2", 768, True, "Cosine")
    assert hash(fp) == hash(EmbedFingerprint("granite-embedding-311m-r2", 768, True, "Cosine"))
    assert len({fp, EmbedFingerprint("granite-embedding-311m-r2", 768, True, "cosine")}) == 1
    assert fp.to_dict() == {
        "model_id": "granite-embedding-311m-r2",
        "dim": 768,
        "normalize": True,
        "distance": "cosine",
        "query_template_hash": "template:absent:v1",
        "doc_template_hash": "template:absent:v1",
    }


# ---------------------------------------------------------------------------
# 2. from_dict strict — missing keys, wrong types, empty model, dim<=0, dim=True, normalize not bool
# ---------------------------------------------------------------------------
def test_from_dict_strict():
    # missing key
    with pytest.raises(FingerprintError):
        EmbedFingerprint.from_dict({"model_id": "m", "dim": 768, "normalize": True})
    # empty model_id
    with pytest.raises(FingerprintError):
        EmbedFingerprint.from_dict({"model_id": "", "dim": 768, "normalize": True, "distance": "cosine"})
    # dim == 0
    with pytest.raises(FingerprintError):
        EmbedFingerprint.from_dict({"model_id": "m", "dim": 0, "normalize": True, "distance": "cosine"})
    # dim is bool (True)
    with pytest.raises(FingerprintError):
        EmbedFingerprint.from_dict({"model_id": "m", "dim": True, "normalize": True, "distance": "cosine"})
    # normalize is string "yes"
    with pytest.raises(FingerprintError):
        EmbedFingerprint.from_dict({"model_id": "m", "dim": 768, "normalize": "yes", "distance": "cosine"})
    # extra keys are ignored (acceptable behaviour) – no error expected
    fp = EmbedFingerprint.from_dict({
        "model_id": "m", "dim": 768, "normalize": True, "distance": "cosine",
        "query_template_hash": "template:absent:v1",
        "doc_template_hash": "template:absent:v1", "x": 1,
    })
    assert fp == EmbedFingerprint("m", 768, True, "cosine")


# ---------------------------------------------------------------------------
# 3. fingerprint_from_slot
# ---------------------------------------------------------------------------
def test_fingerprint_from_slot():
    res = SlotResolution(
        slot_name="embed_text",
        model="granite",
        backend="lmstudio",
        endpoint="http://x",
        embedding_dimension=768,
    )
    expected = EmbedFingerprint("granite", 768, True, "cosine")
    assert fingerprint_from_slot(res) == expected

    # None dimension
    res_none = SlotResolution(
        slot_name="embed_text", model="granite", backend="lmstudio",
        endpoint="http://x", embedding_dimension=None,
    )
    with pytest.raises(FingerprintError):
        fingerprint_from_slot(res_none)

    # empty model
    res_empty = SlotResolution(
        slot_name="embed_text", model="", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
    )
    with pytest.raises(FingerprintError):
        fingerprint_from_slot(res_empty)

    # non-string model must fail-closed as FingerprintError, not AttributeError
    res_int = SlotResolution(
        slot_name="embed_text", model=123, backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
    )
    with pytest.raises(FingerprintError):
        fingerprint_from_slot(res_int)


# ---------------------------------------------------------------------------
# 3b. model_id Unicode equivalence — NFC normalization prevents false-positive
#     FingerprintMismatch when the same model name is encoded as composed vs.
#     decomposed code points (e.g., é as U+00E9 vs. e + U+0301).
# ---------------------------------------------------------------------------
def test_model_id_unicode_nfc_normalization():
    composed = "mod\u00e8le-768"      # NFC: Latin Small Letter E with Grave
    decomposed = "mod\u0065\u0300le-768"  # NFD: e + combining grave
    assert composed != decomposed
    fp_composed = EmbedFingerprint(composed, 768, True, "cosine")
    fp_decomposed = EmbedFingerprint(decomposed, 768, True, "cosine")
    # both canonicalize to NFC
    assert fp_composed.model_id == fp_decomposed.model_id
    assert fp_composed == fp_decomposed
    assert hash(fp_composed) == hash(fp_decomposed)
    assert assert_compatible(fp_composed, fp_decomposed) is None


# ---------------------------------------------------------------------------
# 4. Same-dim model swap – fail closed
# ---------------------------------------------------------------------------
def test_same_dim_model_swap_fail_closed():
    reg = EmbedFingerprint("granite", 768, True, "cosine")
    live = EmbedFingerprint("other-768-model", 768, True, "cosine")
    with pytest.raises(FingerprintMismatch) as ei:
        assert_compatible(reg, live)
    assert "model_id" in str(ei.value)
    assert "granite" in str(ei.value) and "other-768-model" in str(ei.value)
    assert "model_id" in ei.value.fields


# ---------------------------------------------------------------------------
# 5. Dim swap – fail closed
# ---------------------------------------------------------------------------
def test_dim_swap_fail_closed():
    reg = EmbedFingerprint("m", 768, True, "cosine")
    live = EmbedFingerprint("m", 2560, True, "cosine")
    with pytest.raises(FingerprintMismatch) as ei:
        assert_compatible(reg, live)
    assert "dim" in str(ei.value)


# ---------------------------------------------------------------------------
# 6. Normalize/distance mismatch – fail closed; canonical distance passes
# ---------------------------------------------------------------------------
def test_normalize_and_distance_mismatch():
    base = EmbedFingerprint("m", 768, True, "cosine")
    # different normalize
    with pytest.raises(FingerprintMismatch):
        assert_compatible(base, EmbedFingerprint("m", 768, False, "cosine"))
    # different distance
    with pytest.raises(FingerprintMismatch):
        assert_compatible(base, EmbedFingerprint("m", 768, True, "dot"))
    # "Cosine" vs "cosine" – should NOT raise
    assert_compatible(base, EmbedFingerprint("m", 768, True, "Cosine")) is None


# ---------------------------------------------------------------------------
# 7. Matching fingerprint passes
# ---------------------------------------------------------------------------
def test_matching_passes():
    fp = EmbedFingerprint("m", 768, True, "cosine")
    assert assert_compatible(fp, fp) is None


# ---------------------------------------------------------------------------
# 8. stamp_meta – non-mutating, preserves other keys
# ---------------------------------------------------------------------------
def test_stamp_meta_nonmutating_preserves():
    meta = {"note": "keepme"}
    fp = EmbedFingerprint("m", 768, True, "cosine")
    out = stamp_meta(meta, fp)
    assert out["embed"] == fp.to_dict()
    assert out["note"] == "keepme"
    assert "embed" not in meta          # input unchanged
    # stamp_meta(None, fp)
    assert stamp_meta(None, fp) == {"embed": fp.to_dict()}


# ---------------------------------------------------------------------------
# 9. read_meta_fingerprint
# ---------------------------------------------------------------------------
def test_read_meta_fingerprint():
    fp = EmbedFingerprint("m", 768, True, "cosine")
    assert read_meta_fingerprint({"note": "x"}) is None
    assert read_meta_fingerprint(None) is None
    meta = stamp_meta({}, fp)
    assert read_meta_fingerprint(meta) == fp
    # malformed embed
    with pytest.raises(FingerprintError):
        read_meta_fingerprint({"embed": {"model_id": "m"}})


# ---------------------------------------------------------------------------
# 10. assert_slot_matches_registry
# ---------------------------------------------------------------------------
def test_assert_slot_matches_registry():
    fp_reg = EmbedFingerprint("granite", 768, True, "cosine")
    meta = stamp_meta({}, fp_reg)
    res_match = SlotResolution(
        slot_name="embed_text", model="granite", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
    )
    # matching
    assert assert_slot_matches_registry(meta, res_match) == fp_reg
    # same dim, different model → mismatch
    res_swap = SlotResolution(
        slot_name="embed_text", model="other", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
    )
    with pytest.raises(FingerprintMismatch):
        assert_slot_matches_registry(meta, res_swap)
    # missing stamp, require_stamp=True
    unstamped_meta = {"note": "x"}
    with pytest.raises(FingerprintMissing):
        assert_slot_matches_registry(unstamped_meta, res_match, require_stamp=True)
    # missing stamp, require_stamp=False → returns None
    assert assert_slot_matches_registry(unstamped_meta, res_match, require_stamp=False) is None


# ---------------------------------------------------------------------------
# 11. sentinel_vector
# ---------------------------------------------------------------------------
def test_sentinel_vector():
    v = sentinel_vector(768)
    assert len(v) == 768
    assert any(abs(x) > 1e-9 for x in v)
    assert v[0] == 1.0
    assert v[1] == 0.0
    # dim <= 0 raises
    with pytest.raises(FingerprintError):
        sentinel_vector(0)


# ---------------------------------------------------------------------------
# 12. write_sentinel / read_sentinel (mocked client)
# ---------------------------------------------------------------------------
def test_write_and_read_sentinel_mocked():
    client = MagicMock()
    fp = EmbedFingerprint("granite", 768, True, "cosine")
    write_sentinel(client, "_t", fp)
    assert client.upsert.call_count == 1
    point = client.upsert.call_args.kwargs["points"][0]
    assert point.id == SENTINEL_POINT_ID
    assert len(point.vector) == 768
    assert point.payload[SENTINEL_PAYLOAD_KEY] == fp.to_dict()
    assert point.payload[SENTINEL_MARKER_KEY] is True

    # read_sentinel with retrieved point
    retrieved = SimpleNamespace(payload={SENTINEL_PAYLOAD_KEY: fp.to_dict()})
    client.retrieve.return_value = [retrieved]
    assert read_sentinel(client, "_t") == fp

    # empty retrieve → None
    client.retrieve.return_value = []
    assert read_sentinel(client, "_t") is None


# ---------------------------------------------------------------------------
# 13. assert_sentinel_matches / ensure_sentinel (mocked)
# ---------------------------------------------------------------------------
def test_assert_sentinel_and_ensure_mocked():
    client = MagicMock()
    fp = EmbedFingerprint("granite", 768, True, "cosine")

    # ensure_sentinel writes when absent
    client.retrieve.return_value = []
    ensure_sentinel(client, "_t", fp)
    assert client.upsert.call_count == 1

    # now present – should not write again
    client.retrieve.return_value = [
        SimpleNamespace(payload={SENTINEL_PAYLOAD_KEY: fp.to_dict()})
    ]
    client.upsert.reset_mock()
    ensure_sentinel(client, "_t", fp)
    assert client.upsert.call_count == 0

    # assert_sentinel_matches passes
    assert assert_sentinel_matches(client, "_t", fp) is None

    # drifted stored fp
    client.retrieve.return_value = [
        SimpleNamespace(payload={
            SENTINEL_PAYLOAD_KEY: EmbedFingerprint("other", 768, True, "cosine").to_dict()
        })
    ]
    with pytest.raises(FingerprintMismatch):
        assert_sentinel_matches(client, "_t", fp)

    # absent sentinel with require_sentinel=True (default)
    client.retrieve.return_value = []
    with pytest.raises(FingerprintMissing):
        assert_sentinel_matches(client, "_t", fp)


# ---------------------------------------------------------------------------
# 14. Federation round‑trip (real registry, tmp file)
# ---------------------------------------------------------------------------
def test_federation_meta_roundtrip(tmp_path):
    reg_file = tmp_path / "reg.json"
    reg = FederationRegistry(reg_file)
    fp = EmbedFingerprint("granite-embedding-311m-r2", 768, True, "cosine")
    reg.register(
        "wiki",
        "/repos/wiki",
        collection="wiki_chunks",
        dim=768,
        meta=stamp_meta({"note": "vault"}, fp),
    )
    reg.save()
    reg2 = FederationRegistry(reg_file)
    reg2.load()
    resolved = reg2.resolve("wiki")
    # meta.embed round‑trips
    assert read_meta_fingerprint(resolved.meta) == fp
    # note key survives
    assert resolved.meta["note"] == "vault"
    # same‑dim model swap caught by assert_slot_matches_registry
    res_swap = SlotResolution(
        slot_name="embed_text",
        model="DIFFERENT-768-model",
        backend="lmstudio",
        endpoint="http://x",
        embedding_dimension=768,
    )
    with pytest.raises(FingerprintMismatch):
        assert_slot_matches_registry(resolved.meta, res_swap)


# ---------------------------------------------------------------------------
# 15. ensure_sentinel is FAIL-CLOSED on drift (present-but-mismatched stored fp).
#     (r1 audit gap: the ensure_sentinel mismatch branch was only covered via
#      assert_sentinel_matches, never through ensure_sentinel itself.)
# ---------------------------------------------------------------------------
def test_ensure_sentinel_fail_closed_on_drift():
    client = MagicMock()
    fp = EmbedFingerprint("granite", 768, True, "cosine")
    # a stored sentinel with a DIFFERENT (same-dim) model id is present
    client.retrieve.return_value = [
        SimpleNamespace(payload={
            SENTINEL_PAYLOAD_KEY: EmbedFingerprint("other-768-model", 768, True, "cosine").to_dict()
        })
    ]
    with pytest.raises(FingerprintMismatch):
        ensure_sentinel(client, "_t", fp)
    # fail-closed: it must NOT overwrite the live store's sentinel on a mismatch
    assert client.upsert.call_count == 0


# ---------------------------------------------------------------------------
# 16. assert_sentinel_matches soft path: require_sentinel=False on an absent
#     sentinel returns None (no raise) — r1 audit gap.
# ---------------------------------------------------------------------------
def test_assert_sentinel_matches_require_false_absent():
    client = MagicMock()
    fp = EmbedFingerprint("granite", 768, True, "cosine")
    client.retrieve.return_value = []
    assert assert_sentinel_matches(client, "_t", fp, require_sentinel=False) is None


# ---------------------------------------------------------------------------
# 17. read_sentinel handles a RAW DICT point payload (not just a record object);
#     a PRESENT-but-malformed payload is FAIL-CLOSED (raises FingerprintError),
#     distinct from a genuinely ABSENT point (None). (r1 dict-branch gap +
#     r2 fail-open finding: a corrupted present sentinel must NOT collapse to None.)
# ---------------------------------------------------------------------------
def test_read_sentinel_raw_dict_payload():
    client = MagicMock()
    fp = EmbedFingerprint("granite", 768, True, "cosine")
    # a raw dict point with a valid dict payload -> parses
    client.retrieve.return_value = [{"payload": {SENTINEL_PAYLOAD_KEY: fp.to_dict()}}]
    assert read_sentinel(client, "_t") == fp
    # genuinely ABSENT (no point) -> None
    client.retrieve.return_value = []
    assert read_sentinel(client, "_t") is None
    # PRESENT point but payload missing the sentinel key -> FAIL-CLOSED (corruption)
    client.retrieve.return_value = [{"payload": {"other": 1}}]
    with pytest.raises(FingerprintError):
        read_sentinel(client, "_t")
    # PRESENT point but payload is None -> FAIL-CLOSED (corruption / tampering)
    client.retrieve.return_value = [SimpleNamespace(payload=None)]
    with pytest.raises(FingerprintError):
        read_sentinel(client, "_t")


# ---------------------------------------------------------------------------
# 18. ensure_sentinel must NOT silently overwrite a PRESENT-but-corrupted
#     sentinel (r2 fail-open finding): a point at the reserved id with a lost
#     payload is a corruption signal, not "absent" — ensure_sentinel fails closed.
# ---------------------------------------------------------------------------
def test_ensure_sentinel_fail_closed_on_corrupted_present_sentinel():
    client = MagicMock()
    fp = EmbedFingerprint("granite", 768, True, "cosine")
    # a point EXISTS at the sentinel id but its fingerprint payload is gone
    client.retrieve.return_value = [SimpleNamespace(payload={"_bobclaw_sentinel": True})]
    with pytest.raises(FingerprintError):
        ensure_sentinel(client, "_t", fp)
    # fail-closed: it must NOT stamp over the corrupted sentinel
    assert client.upsert.call_count == 0
    # assert_sentinel_matches likewise surfaces the corruption (not a masked "missing")
    with pytest.raises(FingerprintError):
        assert_sentinel_matches(client, "_t", fp)


# ---------------------------------------------------------------------------
# 19. Template identity is part of the fingerprint. A configured template
#     change is drift; an absent template is distinct from an empty template;
#     legacy four-field stamps parse as drift rather than malformed metadata.
# ---------------------------------------------------------------------------
def test_template_identity_change_and_legacy_stamp_fail_closed():
    base_slot = SlotResolution(
        slot_name="embed_text", model="m", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
    )
    query_changed_slot = SlotResolution(
        slot_name="embed_text", model="m", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
        query_instruction_template="query: {text}",
    )
    doc_changed_slot = SlotResolution(
        slot_name="embed_text", model="m", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
        doc_instruction_template="document: {text}",
    )
    empty_query_slot = SlotResolution(
        slot_name="embed_text", model="m", backend="lmstudio",
        endpoint="http://x", embedding_dimension=768,
        query_instruction_template="",
    )
    base = fingerprint_from_slot(base_slot)

    with pytest.raises(FingerprintMismatch) as query_drift:
        assert_compatible(base, fingerprint_from_slot(query_changed_slot))
    assert "query_template_hash" in query_drift.value.fields

    with pytest.raises(FingerprintMismatch) as doc_drift:
        assert_compatible(base, fingerprint_from_slot(doc_changed_slot))
    assert "doc_template_hash" in doc_drift.value.fields

    with pytest.raises(FingerprintMismatch):
        assert_compatible(base, fingerprint_from_slot(empty_query_slot))

    legacy = base.to_dict()
    legacy.pop("query_template_hash")
    legacy.pop("doc_template_hash")
    legacy_fp = EmbedFingerprint.from_dict(legacy)
    with pytest.raises(FingerprintMismatch) as legacy_drift:
        assert_compatible(legacy_fp, base)
    assert set(legacy_drift.value.fields) == {
        "query_template_hash", "doc_template_hash",
    }
