"""
BoBClaw — Worker fan-out single-call wrapper (handoff 006).

`worker_node` runs ONE backend call and writes the result into `worker_results`.
It receives a sub-state via Send, NOT the full AgentState. It does not write
`messages` (that's `join_node`'s job).

Handles timeout via ``asyncio.wait_for`` (handoff 006), 429 detection
(handoff 006), cost-cap pre-check (handoff 007), and critic gating (handoff 008+).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from core.build.contracts import build_impl_prompt, extract_func, is_safe_impl
from core.config import RESEARCH_MAX_ROUNDS, WORKER_TIMEOUT_SECONDS, config
from core.ledger.types import OVERSPEND_TRIGGER
from core.nodes.budget_runtime import branch_spend_result, measure_spend
from core.nodes.critic import run_critic
from core.nodes.execute import _send_to_backend
from core.nodes.gate import WORKER_SCOPE_REVIEW_PROMPT
from core.permissions import Scope
from core.research.subagent import run_iterresearch
from core.telemetry.emit import KIND_WORKER_STATE, emit_event
from core.telemetry.flight import resolve_flight_id

if TYPE_CHECKING:
    pass

_fanout_logger = logging.getLogger("bobclaw.core.fanout")


async def _emit_worker_state(
    sub_state: dict, *, idx: int, backend: str, status: str, **extra,
) -> None:
    """Flight substrate L0.2 — emit one ``worker_state`` monitor frame (per-worker
    STATE-CHANGE, §7.2). Fail-safe (``emit_event`` never raises). The flight resolves
    two-tier from the Send sub-state (explicit ``flight_id`` when the turn threaded one,
    else ambient). ``role`` is the worker face when present (chat fan-out threads
    ``face_id``; the build/research fan-out does not)."""
    await emit_event(
        KIND_WORKER_STATE,
        resolve_flight_id(sub_state),
        {"idx": idx, "role": sub_state.get("face_id"), "backend": backend,
         "status": status, **extra},
    )


def _looks_like_rate_limit(exc: Exception) -> bool:
    """Heuristic: is this a 429 / rate-limit exception?"""
    if isinstance(exc, asyncio.TimeoutError):
        return False
    if isinstance(exc, aiohttp.ClientResponseError) and exc.status == 429:
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in ("429", "rate limit", "too many requests", "rate_limit"))


def _meter_branch(sub_state: dict, entry: dict, *, messages, response: str) -> None:
    """MS-4 BIND-02 — guarded IN-BRANCH spend metering (O(0), no shared poll).

    When the Send carries a ``branch_budget`` (budget active for this branch), measure
    THIS branch's real spend (real provider usage metadata when the seam exposes it,
    else the actual request + response text that crossed the wire) against ITS OWN
    reservation, and record the per-branch breaker / overspend verdict on
    ``entry["budget"]``. Reads ONLY this Send's own payload + this branch's own ``entry``
    — never a global / sibling / shared-balance poll (§2.9 BIND-02). A NO-OP when
    ``branch_budget`` is absent, so a non-budgeted worker turn is byte-identical.
    """
    bb = sub_state.get("branch_budget")
    if not isinstance(bb, dict):
        return
    spent = measure_spend(messages, response, entry.get("usage"))
    entry["budget"] = branch_spend_result(
        bb.get("reservation", 0), spent, trigger=bb.get("trigger", OVERSPEND_TRIGGER)
    )


async def _build_worker(sub_state: dict) -> dict:
    """Build pipeline (Feature 2): implement ONE contract → a ``build_impls`` delta.

    Returns ``{"build_impls": [{idx, name, source, status, ...}]}`` (the reducer
    field join merges by name). A failed/garbled/timed-out worker yields
    ``source=None`` (status records why) so join keeps the contract's stub — a bad
    reply never breaks the build. Writes ``build_impls``, NOT ``worker_results``, so
    it never collides with the chat fan-out's reducer.
    """
    contract = sub_state["build_contract"]
    backend = sub_state.get("backend", "local")
    idx = sub_state.get("subtask_idx", 0)
    name = contract.get("name", "")
    start = time.monotonic()
    source: str | None = None
    # agy_code / codex_code / pi_code need the provider/model threaded through as the
    # model_override (the only posture knob the stateless build fan-out can apply);
    # absent ⇒ the 2-arg call, byte-identical for HTTP backends (deepseek/glm/…).
    worker_model = (
        (sub_state.get("agy_posture") or {}).get("model")
        or (sub_state.get("codex_posture") or {}).get("model")
        or (sub_state.get("pi_posture") or {}).get("model")
    )
    prompt_msgs = [{"role": "user", "content": build_impl_prompt(contract)}]
    # L0.2: emit "running" for this build branch before the backend call. Fail-safe.
    await _emit_worker_state(sub_state, idx=idx, backend=backend, status="running", name=name)
    try:
        send_coro = (
            _send_to_backend(prompt_msgs, backend, worker_model)
            if worker_model
            else _send_to_backend(prompt_msgs, backend)
        )
        response = await asyncio.wait_for(send_coro, timeout=WORKER_TIMEOUT_SECONDS)
        source = extract_func(str(response), name)
        if source:
            # P3 sandbox gate: reject an impl that breaks the pure/stdlib-only/no-I/O
            # contract (disallowed import or dangerous builtin) BEFORE it is written/run
            # → keep the stub so it surfaces as a failing unit, never silent.
            safe, reason = is_safe_impl(source)
            if safe:
                status = "ok"
            else:
                source, status = None, f"unsafe: {reason}"
        else:
            status = "no_impl"
    except asyncio.TimeoutError:
        status = "timeout"
    except Exception as exc:  # noqa: BLE001 — one worker failing must not sink the wave
        status = "rate_limit" if _looks_like_rate_limit(exc) else "failed"
    duration_ms = int((time.monotonic() - start) * 1000)
    entry = {
        "idx": idx, "name": name, "source": source, "status": status,
        "backend_used": backend, "duration_ms": duration_ms,
    }
    # ── Worker-audit tier (advisory) ──────────────────────────────────────────────
    # Review a good impl for spec adherence / scope / quality via the team's critic
    # chain (e.g. minimax → kimi_code → deepseek_v4). ADVISORY: the verdict is recorded
    # for the conductor/human/repair loop; Docker verify stays the hard gate, so an LLM
    # audit can never silently drop a build-green impl. No critic_backend ⇒ skipped
    # (byte-identical for teams without a critic role).
    critic_backend = sub_state.get("critic_backend")
    if critic_backend and status == "ok" and source:
        audit_task = (
            f"Contract: {name}{contract.get('signature', '')}\n"
            f"Spec: {contract.get('doc', '')}"
        )
        try:
            verdict, reasons = await run_critic(
                subtask_text=audit_task,
                worker_output=source,
                critic_backend=critic_backend,
                fallback_chain=sub_state.get("critic_fallback_chain"),
            )
            entry["audit_backend"] = critic_backend
            entry["audit_verdict"] = verdict          # approve | flag | reject | none
            entry["audit_reasons"] = reasons
        except Exception as exc:  # noqa: BLE001 — an audit must never sink a good build
            entry["audit_verdict"] = "none"
            entry["audit_reasons"] = [f"audit_unavailable: {type(exc).__name__}: {exc}"]
    # MS-4 BIND-02: meter this build branch's spend in-branch (guarded; no-op without budget).
    _meter_branch(
        sub_state, entry,
        messages=[{"content": build_impl_prompt(contract)}], response=str(source or ""),
    )
    _fanout_logger.info(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "build_worker", "worker_idx": idx, "name": name,
        "status": status, "filled": source is not None,
        "duration_ms": duration_ms, "backend": backend,
        "audit_verdict": entry.get("audit_verdict"),
    }, separators=(",", ":")))
    # L0.2: emit the terminal build-branch state with a token estimate. measure_spend is
    # a post-hoc measurement of the REAL I/O (the build prompt + the extracted impl), honest
    # even when the backend exposes no usage metadata — the SAME guarded pattern the
    # chat/research worker uses (below), so EVERY terminal worker frame carries `tokens`.
    # No per-backend USD table exists, so this token tick is the truthful per-flight
    # resource signal (the monitor sums it). Fail-soft to 0 — never break a worker on a
    # metering estimate. `running` frames stay token-less (no output yet).
    _tokens = 0
    try:
        _tokens = measure_spend(prompt_msgs, str(source or ""), None)
    except Exception:  # noqa: BLE001 — never break a worker on a metering estimate
        _tokens = 0
    await _emit_worker_state(
        sub_state, idx=idx, backend=backend, status=status,
        duration_ms=duration_ms, tokens=_tokens, name=name,
    )
    return {"build_impls": [entry]}


async def _research_worker(sub_state: dict) -> dict:
    """Research subagent branch (MS2-R3): run the IterResearch loop for ONE research Send.

    A ``research_subagent`` Send carries a spec ``{question, retriever, report_store, max_rounds?,
    instructions?, round_parser?}``. Runs ``run_iterresearch`` (the floor model via the uniform
    ``_send_to_backend``) and returns a ``worker_results`` entry whose ``content`` is the ≤2k CONDENSED return
    — the ONLY large field that crosses to the orchestrator (the §2.5 condensed-return firewall). The internal
    burn is metered against this branch's MS-4 reservation IN-BRANCH (BIND-02) via ``_meter_branch`` (fed the
    REAL internal burn through ``entry["usage"]``); a runaway round already tripped its own breaker inside the
    loop. A dead/timed-out subagent surfaces as a retryable tool-call status (cattle-retry, MS-6), never an
    unhandled raise. Writes ``worker_results`` (NOT ``build_impls``), so it shares the chat fan-out's reducer.

    The ``research_subagent`` spec is TRUSTED-INTERNAL — the research orchestrator constructs it as a plain
    dict; the only field validation (e.g. ``max_rounds`` > 0) is run_iterresearch's construction check.
    """
    spec = sub_state["research_subagent"]

    def _spec(key, default=None):
        # a falsy/missing optional (max_rounds/instructions) falls back to the default below; the real gate on
        # an INVALID value (e.g. max_rounds<=0) is run_iterresearch's construction validation (upstream).
        return spec.get(key, default) if isinstance(spec, dict) else getattr(spec, key, default)

    backend = sub_state.get("backend", "local")
    idx = sub_state.get("subtask_idx", 0)
    question = _spec("question", "") or ""
    start = time.monotonic()
    try:
        cr, _traces = await asyncio.wait_for(
            run_iterresearch(
                question=question,
                retriever=_spec("retriever"),
                model_send=_send_to_backend,
                backend=backend,
                report_store=_spec("report_store"),
                branch_budget=sub_state.get("branch_budget"),
                max_rounds=_spec("max_rounds") or RESEARCH_MAX_ROUNDS,
                round_parser=_spec("round_parser"),
                instructions=_spec("instructions", "") or "",
            ),
            timeout=WORKER_TIMEOUT_SECONDS,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        entry = {
            "idx": idx,
            "text": question,
            "status": "ok",
            # the ≤2k CONDENSED return — the ONLY large field that crosses to the orchestrator (firewall).
            "content": cr.to_content(),
            # the REAL internal burn feeds _meter_branch (the per-branch budget verdict reflects the
            # subagent's true spend, not just the question + condensed-return text).
            "usage": {"total_tokens": cr.internal_burn_tokens},
            "rounds": cr.rounds,
            "internal_burn_tokens": cr.internal_burn_tokens,
            "return_tokens": cr.return_tokens,
            "breaker_tripped": cr.breaker_tripped,
            "duration_ms": duration_ms,
            "backend_used": backend,
        }
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - start) * 1000)
        entry = {
            "idx": idx, "text": question, "status": "timeout",
            "error": f"exceeded {WORKER_TIMEOUT_SECONDS}s",
            "duration_ms": duration_ms, "backend_used": backend,
        }
    except Exception as exc:  # noqa: BLE001 — a dead subagent surfaces as a retryable status, never sinks the wave
        duration_ms = int((time.monotonic() - start) * 1000)
        entry = {
            "idx": idx, "text": question, "status": "failed",
            "error": "rate_limit" if _looks_like_rate_limit(exc) else str(exc),
            "duration_ms": duration_ms, "backend_used": backend,
        }
    # MS-4 BIND-02: record this branch's per-branch spend verdict on entry["budget"] (guarded; no-op without
    # branch_budget). measure_spend reads entry["usage"].total_tokens (the real internal burn) when present.
    _meter_branch(sub_state, entry, messages=[{"content": question}], response=str(entry.get("content") or ""))
    _fanout_logger.info(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "research_worker", "worker_idx": idx, "status": entry["status"],
        "rounds": entry.get("rounds"), "internal_burn_tokens": entry.get("internal_burn_tokens"),
        "return_tokens": entry.get("return_tokens"), "breaker_tripped": entry.get("breaker_tripped"),
        "duration_ms": entry["duration_ms"], "backend": backend,
    }, separators=(",", ":")))
    return {"worker_results": [entry]}


def _prepare_research_spec(sub_state: dict) -> bool:
    """Fill the runtime (non-serializable) pieces of a ``research_subagent`` spec IN PLACE.

    The live seam's marker spec carries only ``question`` (Send args must stay
    msgpack-serializable for the graph checkpointer); the retriever and the per-Send
    ephemeral report store are built here at run time. A spec that already carries them
    (DAG/forest callers, tests) — or a non-dict trusted-internal spec object — is used
    as-is. Returns False when no retriever can be built (env off / availability degrade):
    the caller falls OPEN to the plain chat worker, never a failed subtask.
    """
    spec = sub_state["research_subagent"]
    if not isinstance(spec, dict):
        return True
    prepared = dict(spec)
    if prepared.get("retriever") is None:
        from core.research.wiring import build_research_retriever

        question = str(prepared.get("question") or sub_state.get("task") or "")
        retriever = build_research_retriever(question)
        if retriever is None:
            return False
        prepared["retriever"] = retriever
    if prepared.get("report_store") is None:
        from core.research.subagent import InMemoryReportStore

        prepared["report_store"] = InMemoryReportStore()
    sub_state["research_subagent"] = prepared
    return True


async def worker_node(sub_state: dict) -> dict:
    """Single-worker call. Returns a state delta with one worker_results entry."""
    # L0.4: bind this worker's flight (task-local; each Send runs in its own task) so a
    # metered backend call attributes its USD to the right flight. Covers all branches.
    from core.telemetry.spend import bind_flight_from_state
    bind_flight_from_state(sub_state)
    # ── Build pipeline branch (Feature 2): a build Send carries a contract. ──
    if sub_state.get("build_contract") is not None:
        return await _build_worker(sub_state)
    # ── Research subagent branch (MS2-R3): a research Send carries a subagent spec.
    # Guard-at-top: a non-research worker (no `research_subagent` key) is byte-identical.
    # The LIVE chat seam (hermes wiring) sends a SERIALIZABLE marker spec ({"question": ...}
    # only) — Send args ride through the graph checkpointer, and a retriever callable in
    # them is not msgpack-serializable (live-smoke defect, 2026-07-20). The runtime pieces
    # are built HERE; a spec that already carries retriever/report_store (DAG/forest/tests)
    # is used as-is. Build failure ⇒ fail OPEN to the plain chat worker below. ──
    if sub_state.get("research_subagent") is not None:
        if _prepare_research_spec(sub_state):
            return await _research_worker(sub_state)
    task = sub_state.get("task", "")
    backend = sub_state.get("backend", "local")
    escalation_backend = sub_state.get("escalation_backend")
    subtask_idx = sub_state.get("subtask_idx", 0)

    messages = [{"role": "user", "content": task}]
    recalled = sub_state.get("recalled_facts") or []
    recalled_chunks = (
        sub_state.get("recalled_chunks") or []
        if config.MEMORY_T0_RECALL_ENABLED
        else []
    )
    memory_lines = [f.body.get("text", "") for f in recalled]
    memory_lines.extend(chunk.content for chunk in recalled_chunks)
    if memory_lines:
        bullets = "\n".join(f"- {text}" for text in memory_lines[:5])
        messages.insert(0, {"role": "system", "content": f"Prior context:\n{bullets}"})
    start = time.monotonic()

    # L0.2: emit the mid-run "running" state BEFORE the backend call so a monitor sees
    # the worker light up immediately (not just at join). Fail-safe.
    await _emit_worker_state(sub_state, idx=subtask_idx, backend=backend, status="running")

    try:
        effective_backend = backend
        # agy_code / codex_code / pi_code need the worker face's model threaded through
        # (the only posture knob the stateless fan-out path can apply). Pass it only
        # when set so the common 2-arg call shape is unchanged for other backends.
        worker_model = (
            (sub_state.get("agy_posture") or {}).get("model")
            or (sub_state.get("codex_posture") or {}).get("model")
            or (sub_state.get("pi_posture") or {}).get("model")
        )
        send_coro = (
            _send_to_backend(messages, effective_backend, worker_model)
            if worker_model
            else _send_to_backend(messages, effective_backend)
        )
        response = await asyncio.wait_for(send_coro, timeout=WORKER_TIMEOUT_SECONDS)
        duration_ms = int((time.monotonic() - start) * 1000)
        entry = {
            "idx": subtask_idx,
            "text": task,
            "status": "ok",
            "content": response,
            "usage": {},
            "duration_ms": duration_ms,
            "backend_used": effective_backend,
        }

        critic_backend = sub_state.get("critic_backend")
        if critic_backend:
            scope_data = sub_state.get("scope")
            critic_start = time.monotonic()
            if scope_data:
                # GR-P4: scope-aware worker review (scope-drift gate).
                scope = Scope.model_validate(scope_data)
                scope_json = scope.model_dump_json(indent=2)
                subtask_text = (
                    f"Job scope (declared blast radius):\n{scope_json}\n\n"
                    f"Subtask: {task}"
                )
                verdict, reasons = await run_critic(
                    subtask_text=subtask_text,
                    worker_output=str(response),
                    critic_backend=critic_backend,
                    prompt_template=WORKER_SCOPE_REVIEW_PROMPT,
                )
                entry["critic_backend"] = critic_backend
                entry["critic_verdict"] = verdict
                entry["critic_reasons"] = reasons
                entry["gate_destination"] = (
                    "auto"
                    if verdict == "approve"
                    else "gate"
                    if verdict == "flag"
                    else "human"
                )
                entry["gate_reasons"] = reasons
                if verdict == "reject":
                    entry["status"] = "rejected"
                    entry["error"] = f"critic_rejected: {'; '.join(reasons)}"
                elif verdict in ("flag", "none"):
                    entry["status"] = "flagged"
            else:
                # Existing no-scope behavior: generic correctness critic.
                verdict, reasons = await run_critic(
                    subtask_text=task,
                    worker_output=str(response),
                    critic_backend=critic_backend,
                    prompt_template=sub_state.get("critic_prompt_template"),
                )
                entry["critic_backend"] = critic_backend
                entry["critic_verdict"] = verdict
                entry["critic_reasons"] = reasons
                if verdict == "reject":
                    entry["status"] = "rejected"
                    entry["error"] = f"critic_rejected: {'; '.join(reasons)}"
            critic_duration_ms = int((time.monotonic() - critic_start) * 1000)
            entry["critic_duration_ms"] = critic_duration_ms
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - start) * 1000)
        entry = {
            "idx": subtask_idx,
            "text": task,
            "status": "timeout",
            "error": f"exceeded {WORKER_TIMEOUT_SECONDS}s",
            "duration_ms": duration_ms,
            "backend_used": backend,
        }
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        is_rate_limit = _looks_like_rate_limit(exc)
        entry = {
            "idx": subtask_idx,
            "text": task,
            "status": "failed",
            "error": "rate_limit" if is_rate_limit else str(exc),
            "duration_ms": duration_ms,
            "backend_used": backend,
        }

    # MS-4 BIND-02: meter this branch's spend in-branch (guarded; no-op without budget).
    # Runs on every outcome — ok ⇒ request+response spend; timeout/failed ⇒ request-only
    # (the input WAS sent). Reads only this Send's branch_budget + this branch's entry.
    _meter_branch(
        sub_state, entry, messages=messages, response=str(entry.get("content") or ""),
    )

    log_data = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "turn_id": sub_state.get("turn_id"),
        "worker_idx": subtask_idx,
        "status": entry["status"],
        "duration_ms": entry["duration_ms"],
        "backend": backend,
        "backend_used": entry.get("backend_used"),
        "usage": entry.get("usage"),
    }
    if entry.get("critic_verdict") is not None:
        log_data["critic_backend"] = entry.get("critic_backend")
        log_data["critic_verdict"] = entry["critic_verdict"]
        log_data["critic_duration_ms"] = entry.get("critic_duration_ms")
        log_data["critic_reasons_count"] = len(entry.get("critic_reasons", []))
    _fanout_logger.info(json.dumps(log_data, separators=(",", ":")))

    # L0.2: emit the terminal state-change (ok/failed/timeout/rejected/flagged) with a
    # token estimate (measure_spend = post-hoc measurement of the real I/O, honest even
    # when the backend exposes no usage metadata). No per-backend USD table exists, so a
    # token tick is the truthful per-flight resource signal (the monitor sums it).
    _tokens = 0
    try:
        _tokens = measure_spend(messages, str(entry.get("content") or ""), entry.get("usage"))
    except Exception:  # noqa: BLE001 — never break a worker on a metering estimate
        _tokens = 0
    await _emit_worker_state(
        sub_state, idx=subtask_idx, backend=entry.get("backend_used", backend),
        status=entry["status"], duration_ms=entry.get("duration_ms"), tokens=_tokens,
    )
    return {"worker_results": [entry]}
