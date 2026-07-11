"""MS2-R1 — LKS-first research retriever (own corpus first, web second).

Searches the registered LKS corpus collections FIRST (through the MS-1 federation resolver + the C3
``lks_adapter`` read path) and the web SECOND (a thin, injected search/fetch tool). Returns
``core/verify/entailment.Source`` objects tagged ``SourceKind`` from REAL provenance (LKS payload class /
web-domain class) — it NEVER defaults to PRIMARY (a vendor source mis-tagged PRIMARY would inflate the
downstream PV rate, a false-assurance bug). Exposes the ``retrieve(RetrieveRequest) -> Source | None``
callable the MS-3 entailment gate (R4's CitationAgent) consumes, honoring the ERG's typed ``tried_sources``
negative constraint (exclude already-tried sources, return a strictly decorrelated one).

Additive + self-contained + READ-ONLY: no write path, no network/IO at import; the web tool and the LKS
adapter are duck-typed/injected. Consumes entailment.py / lks_adapter.py / fingerprint.py / exceptions.py;
edits none of them.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence
from urllib.parse import urlsplit

from core.verify.entailment import Source, SourceKind, RetrieveRequest
from core.ledger.federation import FederationError
from core.memory.lks_adapter import ReadAdapterError
from core.memory.fingerprint import FingerprintMissing, FingerprintMismatch
from core.memory.exceptions import ACLViolation, EmbedderUnavailable, RetrievalProviderError

logger = logging.getLogger("bobclaw.research.retrieve")


# ── Exception ──────────────────────────────────────────────────────────────

class ResearchRetrieverError(RuntimeError):
    """Raised for construction/config misuse (empty query, no tier configured, k<=0/web_k<=0)."""
    pass


# ── Provenance constants ───────────────────────────────────────────────────

DEFAULT_LKS_PRIMARY_DIR_SEGMENTS = ("sources",)
DEFAULT_LKS_KIND_KEYS = ("source_kind", "provenance", "kind")
DEFAULT_LKS_TEXT_KEYS = ("chunk_text", "text", "content")

PRIMARY_PROVENANCE_TOKENS = frozenset({"primary", "source", "raw"})
VENDOR_PROVENANCE_TOKENS = frozenset(
    {"vendor", "derived", "synth", "synthesized", "secondary"}
)


# ── Pure classifiers (DEFAULT IS VENDOR — never default PRIMARY) ────────────

def classify_lks_source_kind(
    payload: dict,
    *,
    primary_dir_segments: tuple[str, ...] = DEFAULT_LKS_PRIMARY_DIR_SEGMENTS,
    kind_keys: tuple[str, ...] = DEFAULT_LKS_KIND_KEYS,
) -> SourceKind:
    """Classify an LKS hit payload into PRIMARY or VENDOR (never default PRIMARY).

    Explicit payload stamp wins; else the raw-source path convention; else VENDOR.
    """
    # (1) explicit payload stamp wins
    for key in kind_keys:
        v = payload.get(key)
        if isinstance(v, str):
            t = v.strip().lower()
            if t in PRIMARY_PROVENANCE_TOKENS:
                return SourceKind.PRIMARY
            if t in VENDOR_PROVENANCE_TOKENS:
                return SourceKind.VENDOR
            if t:  # explicit but unrecognised -> VENDOR (never default primary)
                return SourceKind.VENDOR

    # (2) path convention — directory segments only (filename excluded)
    sp = payload.get("source_path")
    if isinstance(sp, str) and sp.strip():
        segs = [s for s in sp.replace("\\", "/").split("/") if s]
        dir_segs = segs[:-1]  # exclude the filename (last segment)
        low = {d.lower() for d in dir_segs}
        if any(seg.lower() in low for seg in primary_dir_segments):
            return SourceKind.PRIMARY

    # (3) default — VENDOR
    return SourceKind.VENDOR


def classify_web_source_kind(
    url: str,
    *,
    primary_domains: tuple[str, ...] = (),
) -> SourceKind:
    """Classify a web result URL into PRIMARY (subdomain-suffix of a primary domain) or VENDOR.

    Default primary_domains=() => all web VENDOR. Never raises (a non-str / unparseable url => VENDOR).
    """
    if not isinstance(url, str):
        return SourceKind.VENDOR
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        # urlsplit raises ValueError on adversarial input (e.g. a NUL byte in the URL) — a search tool can
        # return such a string; fail to VENDOR, never raise (keep the "never default PRIMARY" promise intact).
        return SourceKind.VENDOR
    for d in primary_domains:
        dn = str(d).strip().lower().lstrip(".")
        if not dn:
            continue
        if host == dn or host.endswith("." + dn):
            return SourceKind.PRIMARY
    return SourceKind.VENDOR


# ── Web tier (pluggable; v1 is NOT a crawler) ──────────────────────────────

@dataclasses.dataclass(frozen=True)
class WebResult:
    """A single result from the thin web tool (url + text body + optional title)."""
    url: str
    text: str
    title: str = ""


class WebSearchTool(Protocol):
    """Protocol for the injected second-tier web search tool."""

    async def search(self, query: str, *, k: int = 5) -> list[WebResult]: ...


class CallableWebTool:
    """Wrap any async ``(query, *, k) -> list[WebResult]`` callable as a WebSearchTool."""

    def __init__(self, fn: Callable[..., Awaitable[list[WebResult]]]) -> None:
        self._fn = fn

    async def search(self, query: str, *, k: int = 5) -> list[WebResult]:
        """Delegate to the wrapped async callable."""
        return await self._fn(query, k=k)


# ── Retriever ──────────────────────────────────────────────────────────────

class ResearchRetriever:
    """LKS-first → web-second retriever returning the next untried Source, or None."""

    def __init__(
        self,
        *,
        query: str,
        lks_adapter: Any = None,
        lks_instances: Sequence[str] = (),
        web_tool: Optional[WebSearchTool] = None,
        k: int = 10,
        web_k: int = 5,
        lks_primary_dir_segments: tuple[str, ...] = DEFAULT_LKS_PRIMARY_DIR_SEGMENTS,
        lks_kind_keys: tuple[str, ...] = DEFAULT_LKS_KIND_KEYS,
        lks_text_keys: tuple[str, ...] = DEFAULT_LKS_TEXT_KEYS,
        web_primary_domains: Sequence[str] = (),
        lks_kind_classifier: Optional[Callable[[dict], SourceKind]] = None,
        web_kind_classifier: Optional[Callable[[str], SourceKind]] = None,
        id_prefix: str = "lks",
        propagate_lks_safety: bool = True,
    ) -> None:
        """Bind the query, the LKS read path + instances, the web tier, and the provenance policy."""
        # Validate construction
        if not (isinstance(query, str) and query.strip()):
            raise ResearchRetrieverError("query required")
        instances = tuple(lks_instances)
        if instances and not all(isinstance(x, str) and x.strip() for x in instances):
            raise ResearchRetrieverError("lks_instances entries must be non-empty strings")
        has_lks = lks_adapter is not None and len(instances) > 0
        has_web = web_tool is not None
        if not (has_lks or has_web):
            raise ResearchRetrieverError(
                "no tier configured (need an LKS adapter+instances or a web_tool)"
            )
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ResearchRetrieverError("k must be a positive int")
        if not isinstance(web_k, int) or isinstance(web_k, bool) or web_k <= 0:
            raise ResearchRetrieverError("web_k must be a positive int")
        # id_prefix builds the STABLE source id the gate's tried_sources exclusion keys on — an empty /
        # non-str prefix collapses ids across instances and breaks decorrelation.
        if not (isinstance(id_prefix, str) and id_prefix.strip()):
            raise ResearchRetrieverError("id_prefix must be a non-empty string")
        # lks_text_keys is always consumed by _first_text; the LKS classifier config is consumed only by the
        # DEFAULT classifier (skip its validation when a custom classifier is supplied).
        if not (isinstance(lks_text_keys, (list, tuple)) and lks_text_keys
                and all(isinstance(x, str) and x.strip() for x in lks_text_keys)):
            raise ResearchRetrieverError("lks_text_keys must be a non-empty sequence of non-empty strings")
        if lks_kind_classifier is None and not (
            isinstance(lks_kind_keys, (list, tuple)) and lks_kind_keys
            and all(isinstance(x, str) and x.strip() for x in lks_kind_keys)
            and isinstance(lks_primary_dir_segments, (list, tuple)) and lks_primary_dir_segments
            and all(isinstance(x, str) and x.strip() for x in lks_primary_dir_segments)
        ):
            raise ResearchRetrieverError(
                "lks_kind_keys and lks_primary_dir_segments must be non-empty sequences of non-empty strings"
            )
        # web_primary_domains may be EMPTY (the default => all web VENDOR); if present, it must be a
        # list/tuple of non-empty strings (symmetric config hardening; consumed only by the default web
        # classifier). The sequence-type guard also turns a None into a clear ResearchRetrieverError
        # rather than a TypeError inside the all(...) generator.
        if web_kind_classifier is None and (
            not isinstance(web_primary_domains, (list, tuple))
            or not all(isinstance(x, str) and x.strip() for x in web_primary_domains)
        ):
            raise ResearchRetrieverError(
                "web_primary_domains must be a (possibly empty) sequence of non-empty strings"
            )

        # Store configuration
        self._query: str = query.strip()
        self._lks_adapter = lks_adapter
        self._lks_instances: tuple[str, ...] = instances
        self._web_tool = web_tool
        self._k = k
        self._web_k = web_k
        self._lks_text_keys: tuple[str, ...] = tuple(lks_text_keys)
        self._id_prefix = id_prefix
        self._propagate_lks_safety = propagate_lks_safety

        # Build unary classifiers (defaults bind the module functions to this retriever's config)
        self._lks_kind_classifier = lks_kind_classifier or (
            lambda p: classify_lks_source_kind(
                p,
                primary_dir_segments=tuple(lks_primary_dir_segments),
                kind_keys=tuple(lks_kind_keys),
            )
        )
        self._web_kind_classifier = web_kind_classifier or (
            lambda u: classify_web_source_kind(
                u, primary_domains=tuple(web_primary_domains)
            )
        )

    async def retrieve(self, req: RetrieveRequest) -> Optional[Source]:
        """Return the next untried Source (LKS-first, web-second), excluding req.tried_sources, or None."""
        tried = set(getattr(req, "tried_sources", None) or ())

        # TIER 1 — LKS-first
        if self._lks_adapter is not None:
            for inst in self._lks_instances:
                try:
                    hits = await self._lks_adapter.search(
                        inst, query=self._query, k=self._k
                    )
                except (FingerprintMissing, FingerprintMismatch, ACLViolation):
                    # A same-dim embed swap / non-reader is a corruption / authorization signal — fail
                    # CLOSED; never silently degrade to the web tier.
                    if self._propagate_lks_safety:
                        raise
                    hits = []
                except (
                    EmbedderUnavailable,
                    RetrievalProviderError,
                    ReadAdapterError,
                    FederationError,
                ) as exc:
                    # Availability failures and unknown instances are misses;
                    # continue to the next instance or the web tier.
                    logger.warning(
                        "R1 LKS instance %r unavailable/misconfigured: %s", inst, exc
                    )
                    continue

                for h in hits:
                    payload = getattr(h, "payload", None) or {}
                    text = self._first_text(payload)
                    if not text.strip():
                        continue  # an empty source can't entail — skip
                    sid = self._lks_source_id(inst, h, payload)
                    if sid in tried:
                        continue
                    return Source(
                        id=sid,
                        text=text,
                        kind=self._lks_kind_classifier(payload),
                    )

        # TIER 2 — web-second (consulted only if every LKS tier missed / was already tried)
        if self._web_tool is not None:
            try:
                results = await self._web_tool.search(self._query, k=self._web_k)
            except Exception as exc:  # noqa: BLE001 — a web hiccup is a web-miss, not fatal
                logger.warning("R1 web tool error: %s", exc)
                results = []

            for r in results or []:
                url = str(getattr(r, "url", "") or "")
                text = str(getattr(r, "text", "") or "")
                if not text.strip():
                    continue
                sid = "web:" + url
                if sid in tried:
                    continue
                return Source(
                    id=sid,
                    text=text,
                    kind=self._web_kind_classifier(url),
                )

        return None

    # ── Helpers ────────────────────────────────────────────────────────────

    def _first_text(self, payload: dict) -> str:
        """Return the first non-empty text value from payload across the configured text keys."""
        for key in self._lks_text_keys:
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    def _lks_source_id(self, inst: str, hit: Any, payload: dict) -> str:
        """Build a stable, provenance-keyed LKS source id (chunk_id|chunk_hash|point-id)."""
        raw = (
            payload.get("chunk_id")
            or payload.get("chunk_hash")
            or getattr(hit, "id", "")
        )
        return f"{self._id_prefix}:{inst}:{raw}"


def make_research_retriever(**kwargs: Any) -> Callable[[RetrieveRequest], Awaitable[Optional[Source]]]:
    """Build a ResearchRetriever and return its bound ``.retrieve`` (the exact MS-3 gate callable)."""
    return ResearchRetriever(**kwargs).retrieve
