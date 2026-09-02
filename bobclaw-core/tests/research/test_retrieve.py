import pytest
from typing import Any, Dict, Iterable, List, Optional, Sequence
from unittest.mock import AsyncMock

# Imports under test
from core.research.retrieve import (
    ResearchRetriever,
    ResearchRetrieverError,
    make_research_retriever,
    WebResult,
    CallableWebTool,
    classify_lks_source_kind,
    classify_web_source_kind,
)
from core.verify.entailment import Source, SourceKind, RetrieveRequest, run_entailment_gate, Claim
from core.memory.lks_adapter import ReadAdapterError
from core.ledger.federation import FederationError
from core.memory.fingerprint import FingerprintMismatch, EmbedFingerprint
from core.memory.exceptions import ACLViolation
from core.memory.models import Hit

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def hit(pid: str, score: float = 0.9, **payload) -> Hit:
    """Return a `Hit` with a default chunk_text based on pid if not provided."""
    payload.setdefault("chunk_text", f"text-{pid}")
    return Hit(id=str(pid), score=score, payload=dict(payload))


class FakeLKSAdapter:
    """In-process fake: canned hits per instance, records calls, can raise."""

    def __init__(self, by_instance: Optional[Dict[str, List[Hit]]] = None,
                 raises: Optional[Dict[str, Exception]] = None):
        self.by_instance = by_instance or {}
        self.raises = raises or {}
        self.calls: List[tuple] = []

    async def search(self, instance: str, *, query: str, k: int = 10,
                     filters: Any = None) -> List[Hit]:
        self.calls.append((instance, query, k))
        exc = self.raises.get(instance)
        if exc is not None:
            raise exc
        return list(self.by_instance.get(instance, []))


class FakeWebTool:
    """In-process fake: canned results, records calls, can raise."""

    def __init__(self, results: Optional[List[WebResult]] = None,
                 raises: Optional[Exception] = None):
        self.results = results or []
        self.raises = raises
        self.calls: List[tuple] = []

    async def search(self, query: str, *, k: int = 5) -> List[WebResult]:
        self.calls.append((query, k))
        if self.raises is not None:
            raise self.raises
        return list(self.results)


def req(tried: Iterable[str] = ()) -> RetrieveRequest:
    """Minimal RetrieveRequest for testing (bid_key, tried_sources, no constraint)."""
    return RetrieveRequest(
        bid_key="bk",
        tried_sources=tuple(tried),
        constraint=None,
        reason_code=None,
        attempt=0,
    )


def mkret(**kw: Any) -> ResearchRetriever:
    """Build a ResearchRetriever with query='q' and given overrides."""
    kw.setdefault("query", "q")
    return ResearchRetriever(**kw)


# Convenience fingerprint mismatch factory (exactly 4 args)
def _fingerprint_mismatch() -> FingerprintMismatch:
    return FingerprintMismatch(
        EmbedFingerprint("m-a", 768, True, "cosine"),
        EmbedFingerprint("m-b", 768, True, "cosine"),
        ["model_id"],
        "ctx",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRetrieve:
    """Test suite for `core.research.retrieve`."""

    # .......................................................................
    # 1. LKS-first happy path, no web consulted
    # .......................................................................
    async def test_lks_first_happy_no_web(self) -> None:
        adapter = FakeLKSAdapter(by_instance={"i": [hit("a"), hit("b")]})
        web = FakeWebTool(results=[WebResult("http://w/1", "wtext")])
        ret = mkret(lks_adapter=adapter, lks_instances=["i"], web_tool=web)

        s = await ret.retrieve(req())
        assert isinstance(s, Source)
        # id = f"{prefix}:{instance}:{hit.id}" with default prefix "lks"
        assert s.id == "lks:i:a"
        assert s.text == "text-a"
        assert web.calls == []  # web never called

    # .......................................................................
    # 1b. LKS-first holds under NON-EMPTY tried_sources WITH a web tool present
    #     (a web-eager regression would consult web here — this pins it does NOT)
    # .......................................................................
    async def test_lks_first_with_web_and_nonempty_tried(self) -> None:
        adapter = FakeLKSAdapter(by_instance={"i": [hit("a"), hit("b")]})
        web = FakeWebTool(results=[WebResult("http://w/1", "wtext")])
        ret = mkret(lks_adapter=adapter, lks_instances=["i"], web_tool=web)
        # LKS still has an untried hit (b); the web tier MUST NOT be consulted.
        s = await ret.retrieve(req(tried={"lks:i:a"}))
        assert s.id == "lks:i:b"
        assert web.calls == []

    # .......................................................................
    # 2. LKS provenance never defaults to PRIMARY
    # .......................................................................
    async def test_lks_provenance_never_default_primary(self) -> None:
        # (a) source_path="concepts/x.md" -> VENDOR
        adapter = FakeLKSAdapter(by_instance={"i": [hit("a", source_path="concepts/x.md")]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert s.kind == SourceKind.VENDOR
        assert s.kind != SourceKind.PRIMARY

        # (b) source_path="sources/raw.md" -> PRIMARY
        adapter = FakeLKSAdapter(by_instance={"i": [hit("b", source_path="sources/raw.md")]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert s.kind == SourceKind.PRIMARY

        # (c) explicit "source_kind"="vendor" overrides path "sources/raw.md" -> VENDOR
        adapter = FakeLKSAdapter(by_instance={"i": [hit("c", source_path="sources/raw.md",
                                                         source_kind="vendor")]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert s.kind == SourceKind.VENDOR

        # (d) explicit "provenance"="primary" -> PRIMARY
        adapter = FakeLKSAdapter(by_instance={"i": [hit("d", provenance="primary")]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert s.kind == SourceKind.PRIMARY

        # (e) unrecognized explicit "kind"="whatever" -> VENDOR
        adapter = FakeLKSAdapter(by_instance={"i": [hit("e", kind="whatever")]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert s.kind == SourceKind.VENDOR

    # .......................................................................
    # 3. classify_lks_source_kind unit tests
    # .......................................................................
    def test_classify_lks_unit(self) -> None:
        # empty payload -> VENDOR
        assert classify_lks_source_kind({}) == SourceKind.VENDOR

        # source_path="x.md" (no directory segment) -> VENDOR
        assert classify_lks_source_kind({"source_path": "x.md"}) == SourceKind.VENDOR

        # source_path="sources/a/b.md" -> PRIMARY
        assert classify_lks_source_kind({"source_path": "sources/a/b.md"}) == SourceKind.PRIMARY

        # source_path="a/sources/b.md" -> PRIMARY
        assert classify_lks_source_kind({"source_path": "a/sources/b.md"}) == SourceKind.PRIMARY

        # source_path="sources" (bare filename, no dir) -> VENDOR
        assert classify_lks_source_kind({"source_path": "sources"}) == SourceKind.VENDOR

        # Windows path "sources\\a.md" -> PRIMARY
        assert classify_lks_source_kind({"source_path": "sources\\a.md"}) == SourceKind.PRIMARY

        # explicit source_kind="raw" -> PRIMARY
        assert classify_lks_source_kind({"source_kind": "raw"}) == SourceKind.PRIMARY

        # explicit source_kind="derived" -> VENDOR
        assert classify_lks_source_kind({"source_kind": "derived"}) == SourceKind.VENDOR

        # empty / None source_path -> VENDOR (never default PRIMARY)
        assert classify_lks_source_kind({"source_path": ""}) == SourceKind.VENDOR
        assert classify_lks_source_kind({"source_path": None}) == SourceKind.VENDOR

    # .......................................................................
    # 4. classify_web_source_kind unit tests
    # .......................................................................
    def test_classify_web_unit(self) -> None:
        # default primary_domains=() -> any URL => VENDOR
        assert classify_web_source_kind("https://x.com") == SourceKind.VENDOR

        # with domains
        pd = ("nih.gov",)
        # matches host (www.nih.gov is subdomain of nih.gov)
        assert classify_web_source_kind("https://www.nih.gov/a", primary_domains=pd) == SourceKind.PRIMARY
        # apex domain
        assert classify_web_source_kind("https://nih.gov", primary_domains=pd) == SourceKind.PRIMARY
        # subdomain suffix trick -> VENDOR
        assert classify_web_source_kind("https://evil-nih.gov.attacker.com", primary_domains=pd) == SourceKind.VENDOR
        # not matching
        assert classify_web_source_kind("https://blog.com", primary_domains=pd) == SourceKind.VENDOR

        # malformed URL -> VENDOR, never raises
        assert classify_web_source_kind("not a url", primary_domains=pd) == SourceKind.VENDOR
        # an input urlsplit rejects (invalid IPv6 -> ValueError) -> VENDOR, never propagates
        assert classify_web_source_kind("http://[::1", primary_domains=pd) == SourceKind.VENDOR
        # a port is stripped from the host; an uppercase host is matched case-insensitively
        assert classify_web_source_kind("https://nih.gov:8080/a", primary_domains=pd) == SourceKind.PRIMARY
        assert classify_web_source_kind("https://NIH.GOV/a", primary_domains=pd) == SourceKind.PRIMARY

    # .......................................................................
    # 5. Web provenance never defaults to PRIMARY
    # .......................................................................
    async def test_web_provenance_never_default_primary(self) -> None:
        web = FakeWebTool(results=[WebResult("https://example.gov/p", "wt")])
        ret = mkret(web_tool=web, web_primary_domains=())
        s = await ret.retrieve(req())
        assert s.kind == SourceKind.VENDOR
        assert s.id == "web:https://example.gov/p"

        ret2 = mkret(web_tool=web, web_primary_domains=("example.gov",))
        s2 = await ret2.retrieve(req())
        assert s2.kind == SourceKind.PRIMARY

    # .......................................................................
    # 6. LKS-first then web ordering
    # .......................................................................
    async def test_lks_first_then_web_ordering(self) -> None:
        adapter = FakeLKSAdapter(by_instance={"i": []})  # miss
        web = FakeWebTool(results=[WebResult("http://w/1", "wtext")])
        ret = mkret(lks_adapter=adapter, lks_instances=["i"], web_tool=web)

        s = await ret.retrieve(req())
        assert s.id.startswith("web:")
        assert web.calls == [("q", 5)]  # web called once

    # .......................................................................
    # 7. tried_sources strict decorrelation
    # .......................................................................
    async def test_tried_sources_strict_decorrelation(self) -> None:
        adapter = FakeLKSAdapter(by_instance={"i": [hit("a"), hit("b"), hit("c")]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])

        # No tried sources -> first
        s = await ret.retrieve(req())
        assert s.id == "lks:i:a"
        # Exclude a -> second
        s = await ret.retrieve(req(tried={"lks:i:a"}))
        assert s.id == "lks:i:b"
        # Exclude a,b -> third
        s = await ret.retrieve(req(tried={"lks:i:a", "lks:i:b"}))
        assert s.id == "lks:i:c"
        # All exhausted -> None
        s = await ret.retrieve(req(tried={"lks:i:a", "lks:i:b", "lks:i:c"}))
        assert s is None

        # With a web tool: exhausted LKS -> web tier; strict decorrelation continues WITHIN web; and the
        # terminal "no untried source in EITHER tier -> None" invariant (the gate's failed-attempt path).
        web = FakeWebTool(results=[WebResult("http://w/1", "t1"), WebResult("http://w/2", "t2")])
        ret2 = mkret(lks_adapter=adapter, lks_instances=["i"], web_tool=web)
        all_lks = {"lks:i:a", "lks:i:b", "lks:i:c"}
        s = await ret2.retrieve(req(tried=all_lks))
        assert s.id == "web:http://w/1"
        s = await ret2.retrieve(req(tried=all_lks | {"web:http://w/1"}))
        assert s.id == "web:http://w/2"  # within-web decorrelation
        s = await ret2.retrieve(req(tried=all_lks | {"web:http://w/1", "web:http://w/2"}))
        assert s is None  # both tiers exhausted -> None

    # .......................................................................
    # 8. Multi-instance order
    # .......................................................................
    async def test_multi_instance_order(self) -> None:
        adapter = FakeLKSAdapter(by_instance={
            "i1": [],
            "i2": [hit("z")],
        })
        ret = mkret(lks_adapter=adapter, lks_instances=["i1", "i2"])
        s = await ret.retrieve(req())
        assert s.id == "lks:i2:z"
        # calls order: i1 first, then i2
        assert [c[0] for c in adapter.calls] == ["i1", "i2"]

    # .......................................................................
    # 8b. Cross-instance tried_sources decorrelation (per-hit, not per-instance)
    # .......................................................................
    async def test_cross_instance_decorrelation(self) -> None:
        # i1 has TWO hits; a regressing impl that skips a whole instance once any of its ids is tried
        # would wrongly jump to i2 after a — this pins per-hit filtering WITHIN i1 before advancing.
        adapter = FakeLKSAdapter(by_instance={"i1": [hit("a"), hit("b")], "i2": [hit("c")]})
        web = FakeWebTool(results=[WebResult("http://w", "wt")])
        ret = mkret(lks_adapter=adapter, lks_instances=["i1", "i2"], web_tool=web)
        assert (await ret.retrieve(req())).id == "lks:i1:a"
        assert (await ret.retrieve(req(tried={"lks:i1:a"}))).id == "lks:i1:b"
        assert (await ret.retrieve(req(tried={"lks:i1:a", "lks:i1:b"}))).id == "lks:i2:c"
        # web only after BOTH instances exhausted
        assert web.calls == []
        assert (await ret.retrieve(req(tried={"lks:i1:a", "lks:i1:b", "lks:i2:c"}))).id.startswith("web:")
        # exact per-attempt (i1, i2) call sequence: i1 searched FIRST on every attempt; i2 only after i1
        # is exhausted (never i2-before-i1 within an attempt).
        assert [c[0] for c in adapter.calls] == ["i1", "i1", "i1", "i2", "i1", "i2"]

    # .......................................................................
    # 8c. CallableWebTool bridges a plain async function as a WebSearchTool
    # .......................................................................
    async def test_callable_web_tool_bridge(self) -> None:
        seen = []

        async def fn(query, *, k=5):
            seen.append((query, k))
            return [WebResult("http://w/x", "wt")]

        tool = CallableWebTool(fn)
        ret = ResearchRetriever(query="q", web_tool=tool, web_k=3)
        s = await ret.retrieve(req())
        assert s.id == "web:http://w/x"
        assert seen == [("q", 3)]  # the (query, k) contract bridged through CallableWebTool

    # .......................................................................
    # 9. Empty-text hit skipped
    # .......................................................................
    async def test_empty_text_hit_skipped(self) -> None:
        # hit "a" has whitespace-only chunk_text
        adapter = FakeLKSAdapter(by_instance={
            "i": [
                Hit("a", 0.9, {"chunk_text": "  "}),
                hit("b", 0.8),
            ]
        })
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert s.id == "lks:i:b"  # "a" skipped

        # symmetric: an empty/whitespace-text WebResult is skipped too
        web = FakeWebTool(results=[WebResult("http://a", "   "), WebResult("http://b", "t")])
        retw = mkret(web_tool=web)
        sw = await retw.retrieve(req())
        assert sw.id == "web:http://b"

    # .......................................................................
    # 10. Safety fault propagates fail-closed; opt-out falls to web
    # .......................................................................
    async def test_safety_fault_propagates_fail_closed(self) -> None:
        # FingerprintMismatch -> propagate (default) => raise, web not consulted
        adapter = FakeLKSAdapter(raises={"i": _fingerprint_mismatch()})
        web = FakeWebTool(results=[WebResult("http://w", "wt")])
        ret = mkret(lks_adapter=adapter, lks_instances=["i"], web_tool=web,
                    propagate_lks_safety=True)
        with pytest.raises(FingerprintMismatch):
            await ret.retrieve(req())
        assert web.calls == []

        # ACLViolation -> same behaviour
        adapter2 = FakeLKSAdapter(raises={"i": ACLViolation("inst", "no access")})
        ret2 = mkret(lks_adapter=adapter2, lks_instances=["i"], web_tool=web,
                     propagate_lks_safety=True)
        with pytest.raises(ACLViolation):
            await ret2.retrieve(req())

        # propagate_lks_safety=False -> falls to web
        adapter3 = FakeLKSAdapter(raises={"i": _fingerprint_mismatch()})
        web2 = FakeWebTool(results=[WebResult("http://fallback", "fb")])
        ret3 = mkret(lks_adapter=adapter3, lks_instances=["i"], web_tool=web2,
                     propagate_lks_safety=False)
        s = await ret3.retrieve(req())
        assert s.id.startswith("web:")

        # propagate_lks_safety=False with NO web tool -> the safety fault is an instance-miss -> None
        adapter4 = FakeLKSAdapter(raises={"i": _fingerprint_mismatch()})
        ret4 = mkret(lks_adapter=adapter4, lks_instances=["i"], propagate_lks_safety=False)
        assert await ret4.retrieve(req()) is None

    # .......................................................................
    # 11. Transient ReadAdapterError treated as instance miss
    # .......................................................................
    async def test_transient_readadaptererror_is_instance_miss(self) -> None:
        adapter = FakeLKSAdapter(
            by_instance={"i2": [hit("z")]},
            raises={"i1": ReadAdapterError("conn")},
        )
        ret = mkret(lks_adapter=adapter, lks_instances=["i1", "i2"])
        s = await ret.retrieve(req())
        assert s.id == "lks:i2:z"  # i1 missed gracefully

        # Only i1 (miss) + web -> web result
        web = FakeWebTool(results=[WebResult("http://web/1", "wt")])
        ret2 = mkret(lks_adapter=adapter, lks_instances=["i1"], web_tool=web)
        s2 = await ret2.retrieve(req())
        assert s2.id.startswith("web:")

        # Only i1 (miss) no web -> None
        ret3 = mkret(lks_adapter=adapter, lks_instances=["i1"])
        s3 = await ret3.retrieve(req())
        assert s3 is None

    # .......................................................................
    # 11b. A misconfigured/unknown instance name (FederationError from registry.resolve)
    #      is an instance-miss — it must NEVER escape retrieve() and crash the gate.
    # .......................................................................
    async def test_federation_error_is_instance_miss(self) -> None:
        adapter = FakeLKSAdapter(
            by_instance={"good": [hit("z")]},
            raises={"bad": FederationError("Unknown instance 'bad'")},
        )
        # bad (FederationError) is skipped; good still resolves
        ret = mkret(lks_adapter=adapter, lks_instances=["bad", "good"])
        s = await ret.retrieve(req())
        assert s.id == "lks:good:z"
        # only the bad instance + web -> web (not a crash)
        web = FakeWebTool(results=[WebResult("http://w", "wt")])
        ret2 = mkret(lks_adapter=adapter, lks_instances=["bad"], web_tool=web)
        s2 = await ret2.retrieve(req())
        assert s2.id.startswith("web:")
        # only the bad instance, no web -> None (not a raise)
        ret3 = mkret(lks_adapter=adapter, lks_instances=["bad"])
        assert await ret3.retrieve(req()) is None

    # .......................................................................
    # 12. Construction validation
    # .......................................................................
    async def test_construction_validation(self) -> None:
        # Empty query
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="")
        # No tier: no web_tool and empty lks_instances
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", lks_instances=[])
        # k=0
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", lks_adapter=FakeLKSAdapter(),
                              lks_instances=["x"], k=0)
        # web_k=0
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", web_tool=FakeWebTool(), web_k=0)

        # negative k / web_k
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", web_tool=FakeWebTool(), k=-1)
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", web_tool=FakeWebTool(), web_k=-1)
        # id_prefix must be a non-empty string (it builds the stable tried_sources id)
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", web_tool=FakeWebTool(), id_prefix="")
        # lks_text_keys must be a non-empty sequence of non-empty strings
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", web_tool=FakeWebTool(), lks_text_keys=())
        # lks_instances entries must be non-empty strings
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", lks_adapter=FakeLKSAdapter(), lks_instances=[None])
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", lks_adapter=FakeLKSAdapter(), lks_instances=[""])
        # web_primary_domains must be a sequence (None -> clear error, not a TypeError)
        with pytest.raises(ResearchRetrieverError):
            ResearchRetriever(query="q", web_tool=FakeWebTool(), web_primary_domains=None)

        # Valid web-only retriever
        ret = ResearchRetriever(query="q", web_tool=FakeWebTool([WebResult("http://w", "t")]))
        assert isinstance(ret, ResearchRetriever)

    # .......................................................................
    # 13. Gate compose: tried_sources accumulates distinct ids across attempts
    # .......................................................................
    async def test_gate_compose_tried_sources(self) -> None:
        adapter = FakeLKSAdapter(by_instance={"i": [hit("a"), hit("b"), hit("c")]})
        ret = make_research_retriever(
            query="q",
            lks_adapter=adapter,
            lks_instances=["i"],
        )
        # make_research_retriever returns the exact MS-3 gate callable type:
        # Callable[[RetrieveRequest], Awaitable[Optional[Source]]]
        import inspect
        assert callable(ret) and inspect.iscoroutinefunction(ret)

        # Stub send that always returns NOT_ENTAILED
        async def send_stub(messages, backend):
            return '{"verdict":"not_entailed","reasons":["no"],"reason_code":null}'

        claim = Claim(subject="s", predicate="p", numeric_value=5,
                      cited_source_id="x")
        out = await run_entailment_gate(
            claim=claim,
            retrieve=ret,
            actor_backend="deepseek_v4_flash",
            critic_backend="glm_5_2",
            send=send_stub,
            max_attempts=8,
        )
        assert out.exhausted is True
        # RETRY_LIMIT == 2 => the gate tries EXACTLY two distinct sources before EXHAUSTED_SEARCH; R1
        # returned a DIFFERENT (decorrelated) source on each attempt, excluding the gate-accumulated set.
        from core.ledger.types import RETRY_LIMIT
        tried = out.entry["tried_sources"]
        expected_ids = ["lks:i:a", "lks:i:b", "lks:i:c"]
        assert len(tried) == RETRY_LIMIT
        assert tried == expected_ids[:RETRY_LIMIT]
        assert len(set(tried)) == len(tried)  # all distinct

    # .......................................................................
    # 13b. Gate compose ACROSS tiers: LKS exhausts mid-gate -> web; tried_sources spans BOTH tiers
    # .......................................................................
    async def test_gate_compose_cross_tier_tried_sources(self) -> None:
        # LKS has a single hit so the gate's 2nd attempt (RETRY_LIMIT==2) falls THROUGH to the web tier —
        # proving tried_sources accumulates DISTINCT ids spanning LKS + web within the gate.
        adapter = FakeLKSAdapter(by_instance={"i": [hit("a")]})
        web = FakeWebTool(results=[WebResult("http://w/1", "t1"), WebResult("http://w/2", "t2")])
        ret = make_research_retriever(query="q", lks_adapter=adapter, lks_instances=["i"], web_tool=web)

        async def send_stub(messages, backend):
            return '{"verdict":"not_entailed","reasons":["no"],"reason_code":null}'

        claim = Claim(subject="s", predicate="p", numeric_value=5, cited_source_id="x")
        out = await run_entailment_gate(
            claim=claim, retrieve=ret, actor_backend="deepseek_v4_flash",
            critic_backend="glm_5_2", send=send_stub, max_attempts=8,
        )
        assert out.exhausted is True
        tried = out.entry["tried_sources"]
        assert tried == ["lks:i:a", "web:http://w/1"]  # cross-tier, distinct, LKS-first then web
        assert len(set(tried)) == len(tried)
        # retrieve()-level ordering: the LKS adapter was consulted before the web tool became non-empty
        # (proves LKS-first within retrieve, not merely that the gate threads tried_sources in).
        assert len(adapter.calls) >= 1 and adapter.calls[0][0] == "i"
        assert len(web.calls) == 1  # web consulted exactly once, on the LKS-exhausted attempt

    # .......................................................................
    # 14. Source shape and no mutation
    # .......................................................................
    async def test_returns_source_and_no_mutation(self) -> None:
        payload = {"chunk_text": "t", "source_path": "concepts/x.md"}
        adapter = FakeLKSAdapter(by_instance={"i": [Hit("a", 0.9, payload)]})
        ret = mkret(lks_adapter=adapter, lks_instances=["i"])
        s = await ret.retrieve(req())
        assert isinstance(s, Source)
        # Ensure payload not mutated
        assert payload == {"chunk_text": "t", "source_path": "concepts/x.md"}
        # No write methods exposed (structural read-only — broad write-style surface)
        for attr in ("index", "upsert", "delete", "write", "create_collection",
                     "add", "remove", "update", "insert", "put", "modify", "delete_collection"):
            assert not hasattr(ret, attr)
