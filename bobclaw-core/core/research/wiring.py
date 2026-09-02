"""Research-lane federated LKS wiring — the LIVE seam (hermes-corpus reaches real turns).

Everything below this module already existed and was tested but had NO live caller: the LKS-first
``ResearchRetriever`` (R1), the IterResearch loop (R3, ``worker._research_worker``), and the join.
This module is the missing top wiring: when ``RESEARCH_LKS_INSTANCES`` is non-empty (e.g.
``"hermes"``), the research fan-out edge (``dispatch._route_after_dispatch``) calls
``attach_research_specs`` to turn each research Send into a ``research_subagent`` Send whose
retriever reads the configured federation instances LKS-first. Empty env ⇒ every call here is an
immediate no-op and the graph is byte-identical to today.

This is deliberately the research lane's OWN seam. It must NOT reuse the recall seam
(``MEMORY_LKS_FIRST``/``MEMORY_LKS_INSTANCE``): that path has REPLACEMENT semantics (an LKS hit
replaces BoB's own memory facts for the turn) and exists for BoB's own store consolidation —
pointing it at a foreign corpus is wrong. Adapter CONSTRUCTION is shared with that seam
(``bootstrap.build_lks_read_adapter``) so the hard read posture (reader ``bobclaw``, stamp + ACL
required) can never drift between the two.

Failure split (mirrors the recall seam):
* AVAILABILITY (memory not bootstrapped, registry missing, embedder/Qdrant down, no instance
  survives pre-flight) ⇒ fail OPEN: no spec is attached and the turn runs today's plain-worker
  path. Build failures are NOT cached — a later research turn retries, so a recovered embedder
  re-enables the lane without a restart.
* FINGERPRINT/ACL mismatch at read time ⇒ fail CLOSED: ``ResearchRetriever`` propagates
  (``propagate_lks_safety=True`` default) and the subtask surfaces as a LOUD failure in the join —
  never a silent degrade to a wrong read.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("bobclaw.research.wiring")

_CACHE_LOCK = threading.Lock()
# (adapter, ok_instances) — cached ONLY on a successful build (see module docstring).
_adapter_cache: Optional[tuple[Any, tuple[str, ...]]] = None


def reset_adapter_cache() -> None:
    """Drop the cached adapter (tests / an operator re-pointing the registry at runtime)."""
    global _adapter_cache
    with _CACHE_LOCK:
        _adapter_cache = None


def configured_instances() -> tuple[str, ...]:
    """The RESEARCH_LKS_INSTANCES ids, read from ``core.config`` at call time (monkeypatch-friendly)."""
    from core import config as _config

    return tuple(getattr(_config, "RESEARCH_LKS_INSTANCES", ()) or ())


def _build_adapter(instances: tuple[str, ...]) -> tuple[Any, tuple[str, ...]]:
    """Build (and cache on success) the shared hard-posture LKS read adapter over *instances*.

    Reuses the live memory bundle's slot resolver + Qdrant client — the research lane reads with
    the SAME embedder fingerprint discipline as recall. Memory not bootstrapped / any construction
    failure ⇒ ``(None, ())`` (availability ⇒ fail open, logged by the shared builder or here).
    """
    global _adapter_cache
    with _CACHE_LOCK:
        if _adapter_cache is not None:
            return _adapter_cache

        try:
            from core.memory.bootstrap import get_memory

            mem = get_memory()
        except Exception as exc:  # noqa: BLE001 — no memory bundle ⇒ no LKS tier; the turn still runs
            logger.warning(
                "RESEARCH_LKS_INSTANCES is set (%r) but memory is not bootstrapped (%s: %s); "
                "research LKS reads stay OFF for this turn",
                instances, type(exc).__name__, exc,
            )
            return (None, ())

        from core.memory.bootstrap import build_lks_read_adapter

        qdrant_client = getattr(getattr(mem.retriever, "_provider", None), "_client", None)
        adapter, ok = build_lks_read_adapter(
            mem.slot_resolver, qdrant_client, instances, seam="RESEARCH_LKS_INSTANCES (research lane)"
        )
        if adapter is None:
            return (None, ())
        _adapter_cache = (adapter, ok)
        return _adapter_cache


def build_research_retriever(question: str):
    """A bound LKS-first retrieve callable for *question*, or ``None`` when the lane is OFF.

    Called by ``worker._prepare_research_spec`` at RUN time (never at Send-build time — a
    retriever callable inside Send args breaks the graph checkpointer's msgpack serialization;
    live-smoke defect, 2026-07-20). ``None`` (env empty, blank question, availability degrade)
    means the worker falls open to the plain chat path.
    """
    instances = configured_instances()
    if not instances:
        return None
    if not (isinstance(question, str) and question.strip()):
        return None

    adapter, ok = _build_adapter(instances)
    if adapter is None or not ok:
        return None

    from core.research.retrieve import make_research_retriever

    # No web tool exists in core yet, so the retriever is LKS-only (a valid single-tier
    # configuration); when a web tool lands, thread it here and the tiering is already handled.
    return make_research_retriever(
        query=question,
        lks_adapter=adapter,
        lks_instances=ok,
        web_tool=None,
    )


def research_subagent_spec(question: str) -> Optional[dict]:
    """A FULL runtime ``research_subagent`` spec for *question*, or ``None`` when the lane is OFF.

    Convenience for in-process callers and tests: the question, a bound LKS-first retriever, and
    a fresh per-spec ephemeral report store. The live chat seam does NOT use this — it sends the
    serializable marker (``attach_research_specs``) and the worker builds the runtime pieces.
    """
    if not (isinstance(question, str) and question.strip()):
        return None
    retriever = build_research_retriever(question)
    if retriever is None:
        return None

    from core.research.subagent import InMemoryReportStore

    return {
        "question": question.strip(),
        "retriever": retriever,
        "report_store": InMemoryReportStore(),
    }


def attach_research_specs(args: list[dict]) -> int:
    """Attach a SERIALIZABLE marker spec to each fan-out Send arg (in place); return the count.

    Called by the research branch of ``_route_after_dispatch`` AFTER the plain chat args are
    built. The marker is ``{"question": <arg task>}`` ONLY — Send args ride through the graph
    checkpointer, so nothing non-msgpack-serializable may be attached here; the worker builds
    the retriever + report store at run time and falls open to the plain chat path if it can't.
    Lane OFF (env empty) or a blank task ⇒ that arg is left untouched. Never raises.
    """
    attached = 0
    if not configured_instances():
        return attached
    for arg in args:
        question = str(arg.get("task") or "").strip()
        if not question:
            continue
        arg["research_subagent"] = {"question": question}
        attached += 1
    return attached


__all__ = [
    "attach_research_specs",
    "build_research_retriever",
    "configured_instances",
    "research_subagent_spec",
    "reset_adapter_cache",
]
