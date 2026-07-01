"""
BoBClaw — CoCouncil pre-close grounding gate (P2, fusion path).

``grounding_node`` is the cross-shape **grounding gate** (design §A2): it runs
once, right before the fusion path converges (``synthesize → ground → …``). It
verifies the synthesized answer's LOAD-BEARING factual claims against the **live
web (read-only)** and decides converge-vs-restart:

  1. Build a read-only WebSearch posture and a "extract load-bearing claims →
     verify each against the live web → return a JSON verdict" prompt.
  2. Spawn the verifier via ``ClaudeCodeClient.chat(...)`` DIRECTLY — a one-off
     verify spawn, NOT through the graph's ``execute_node`` / ``_send_to_backend``
     seam (claude_code is a subprocess, not the HTTP backend seam). The posture
     maps to argv exactly like ``execute_node`` maps ``cc_posture``:
     ``{permission_mode: "plan", allowed_tools: ["WebSearch", "WebFetch"]}`` →
     ``--permission-mode plan --allowedTools WebSearch WebFetch`` (plan/read-only
     so it CANNOT edit/scratch).
  3. Parse the verdict with a ``critic.py``-style tolerant ``extract_json``.
  4. Compute the **ratio** drift decision (OPEN-B): restart iff
     ``#contradicted / #claims >= COUNCIL_DRIFT_THRESHOLD``. ``unverifiable``
     claims are flagged in the handoff but do NOT by themselves force a restart.
  5. Decide converge-vs-restart, enforcing the **restart budget**
     (``COUNCIL_RESTART_BUDGET``) and the **global cost ceiling**
     (``COUNCIL_MAX_USD``, fail-loud on breach).

ADDITIVE / fail-open contract:
  * The ``ground`` node is ALWAYS wired (P3b: ``synthesize → ground → …``); the
    on/off decision moved from build-time topology to a RUNTIME gate so a profile
    can flip grounding per-run. ``grounding_enabled(spec)`` (a profile's
    ``protocol_bounds.grounding`` overriding the global ``COUNCIL_GROUND_CADENCE``)
    is read by BOTH this node and ``synthesize_node``; when OFF, synthesize commits
    in-node and this node is a no-op converge (no spawn, no second commit), so the
    answer still lands exactly once — equivalent to the old P1 ``synthesize → END``.
  * Per-run protocol bounds (``max_usd`` / ``restart_budget`` / ``drift_threshold``)
    likewise override the global module constants for this run; unset keys fall
    back to the globals (byte-identical to pre-P3b for profile-less / council-max).
  * Grounding FAILURES (parse / timeout / spawn / unavailable) FAIL OPEN →
    CONVERGE (never restart on a grounding failure), like recall fail-open.
  * On a restart decision this node writes the **re-seed context** onto
    ``council_spec["reseed_context"]`` (OG topic + output-so-far + synth steer +
    grounding research) and increments ``council_restart``; ``_route_after_ground``
    in ``graph.py`` then routes back to ``panel_dispatch`` to re-run round 1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

from core.config import (
    COUNCIL_DRIFT_THRESHOLD,
    COUNCIL_GROUND_BACKEND,
    COUNCIL_MAX_USD,
    COUNCIL_RESTART_BUDGET,
    config,
)

logger = logging.getLogger(__name__)

# Read-only + web grounding posture (design §A2/§C "Chair reads to verify, never
# acts"). ``plan`` permission-mode is the STRICT read-only option (NOT
# scratch-write — no Write/Edit/Bash) and WebSearch/WebFetch give live
# verification. Maps to argv exactly like execute_node maps cc_posture:
#   --permission-mode plan --allowedTools WebSearch WebFetch
GROUNDING_POSTURE: dict = {
    "permission_mode": "plan",
    "allowed_tools": ["WebSearch", "WebFetch"],
}

# Per-grounding-spawn estimated cost (USD). The engine deliberately ships NO
# per-(backend, tokens) price map (the stale price map was dropped in the P1
# port — see panel.make_cost_fn), so the global ceiling is enforced against a
# coarse per-spawn estimate accumulated on ``council_cost_usd``. Env-overridable
# so the ceiling can be tuned without a real meter. A grounding spawn (real
# ``claude`` + live web) is the most expensive single council call, so it
# dominates the estimate; panel/synth contributions are folded in by the nodes
# that own them when a real meter lands (TODO §A3).
GROUNDING_SPAWN_USD: float = float(os.getenv("COUNCIL_GROUNDING_SPAWN_USD", "0.25"))


# Recognized on/off tokens for a profile's protocol_bounds.grounding — the SINGLE
# allowlist shared by runtime coercion (grounding_enabled) and author-time
# validation (teams.validate_profile), so the two never diverge.
GROUNDING_ON_TOKENS: tuple = ("on", "true", "1", "yes", "enabled")
GROUNDING_OFF_TOKENS: tuple = ("off", "false", "0", "no", "none", "disabled", "disable", "")


def grounding_enabled(spec: Optional[dict]) -> bool:
    """The ONE per-run grounding on/off decision (P3b), shared by both call sites:
    ``synthesize_node`` (defer vs commit-in-node) and ``grounding_node`` (run the
    real gate vs no-op converge).

    A profile's ``protocol_bounds.grounding`` (``on``/``off``/bool) overrides the
    global ``COUNCIL_GROUND_CADENCE``; when absent we fall back to the global
    cadence so existing (profile-less / council-max) runs are byte-identical.

    ⚠ Both sites MUST read THIS function, not their own copy of the rule. If
    synthesize defers but ground no-ops (or vice-versa) the final answer is
    double-emitted or dropped (the streaming-drop class flagged at
    ``synthesize.py:17``). Single-sourcing the decision is what keeps the
    defer/commit chokepoint aligned at runtime now that the build-time topology
    gate is gone.
    """
    bounds = (spec or {}).get("bounds") or {}
    g = bounds.get("grounding")
    if g is not None:
        # bool first (YAML true/false), then numeric falsy (0/0.0 = off, so int
        # and float agree), then an explicit on/off token allowlist. An
        # UNRECOGNIZED token (typo, hallucinated synonym, free-text) FAILS SAFE to
        # OFF — the cheaper side — instead of silently selecting the expensive
        # web-verifier path. (Denylist→allowlist hardening, P3b review.)
        if isinstance(g, bool):
            return g
        if isinstance(g, (int, float)):
            return bool(g)
        s = str(g).strip().lower()
        if s in GROUNDING_ON_TOKENS:
            return True
        if s in GROUNDING_OFF_TOKENS:
            return False
        logger.warning("council grounding: unrecognized grounding bound %r; "
                       "treating as OFF (fail-safe)", g)
        return False
    from core.config import COUNCIL_GROUND_CADENCE as _cadence
    return str(_cadence or "").strip().lower() != "off"


# Per-run bound coercion now lives in the shared core.nodes._bounds (so debate.py
# reuses it without a debate→grounding import). Re-exported under the local names
# grounding_node already uses.
from core.nodes._bounds import bound_float as _bound_float  # noqa: E402
from core.nodes._bounds import bound_int as _bound_int  # noqa: E402

# JSON verdict schema (schema-in-prompt, mirrors critic.py's CriticVerdict).
_VERIFY_PROMPT_TEMPLATE: str = """You are the grounding verifier for a ForestOS council session (COUNCIL-OS v1.0, read-only).

The council has synthesized the following answer to the topic below. Your job is NOT to rewrite it. Your job is to verify it against reality.

TOPIC:
{topic}

SYNTHESIZED ANSWER:
{answer}

Do the following:
1. Extract the LOAD-BEARING factual claims — the specific, checkable assertions the answer depends on (names, numbers, dates, capabilities, events). Ignore opinions, recommendations, and hedged statements.
2. For each load-bearing claim, verify it against the LIVE WEB using WebSearch / WebFetch (read-only — do not act, edit, or write).
3. Mark each claim "confirmed" (the web supports it), "contradicted" (the web refutes it), or "unverifiable" (you could not find authoritative support either way).

Respond with a JSON object on a single line and NOTHING else:
{{"claims": [{{"claim": "<the claim>", "status": "confirmed|contradicted|unverifiable", "note": "<one-line evidence/source>"}}], "research": "<brief findings to seed a re-deliberation if the answer drifted from reality>"}}

If the answer contains no load-bearing factual claims (pure reasoning/opinion), return an empty claims list and say so in research."""


def extract_json(text: str) -> Optional[dict]:
    """Tolerant JSON extractor for the grounding verdict (mirrors critic.py).

    Strips a ```json fence, then tries: whole-string parse, then a
    ``"claims"``-anchored object, then the widest ``{...}`` span. Returns None
    when nothing parses (caller fails open → converge).
    """
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()

    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Anchor on the verdict's distinctive key, then widen.
    m = re.search(r'\{.*"claims".*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


def _normalize_claims(data: dict) -> tuple[list[dict], str]:
    """Pull a clean ``(claims, research)`` out of a parsed verdict, tolerantly.

    Drops malformed claim entries and clamps unknown statuses to "unverifiable"
    (fail-safe: an unparseable status never counts as a contradiction). Returns
    ``([], research)`` when ``claims`` is missing/not-a-list.
    """
    raw_claims = data.get("claims")
    research = str(data.get("research") or "").strip()
    claims: list[dict] = []
    if isinstance(raw_claims, list):
        for c in raw_claims:
            if not isinstance(c, dict):
                continue
            status = str(c.get("status") or "").strip().lower()
            if status not in ("confirmed", "contradicted", "unverifiable"):
                status = "unverifiable"
            claims.append(
                {
                    "claim": str(c.get("claim") or "").strip(),
                    "status": status,
                    "note": str(c.get("note") or "").strip(),
                }
            )
    return claims, research


def compute_drift(claims: list[dict], threshold: "float | None" = None) -> dict:
    """The drift formula (OPEN-B = ratio), in ONE documented testable helper.

    ``drift_ratio  = (#contradicted + #unverifiable) / max(1, #claims)``  — the
                     overall "how much of the answer is unsupported" signal,
                     surfaced for logging/handoff.
    ``restart_ratio = #contradicted / max(1, #claims)``  — the DECISION driver:
                     a *contradicted* claim weighs the restart; *unverifiable*
                     alone only flags the handoff (§A2 / resolved OPEN-B).

    ``should_restart`` is ``restart_ratio >= threshold`` AND at least one
    contradiction exists (a 0-contradiction answer never restarts even if the
    threshold is 0). An empty claim list (pure-reasoning answer) → ratios 0.0,
    no restart.

    ``threshold`` is the per-run drift threshold (P3b: a profile's
    ``protocol_bounds.drift_threshold``); when None it falls back to the global
    ``COUNCIL_DRIFT_THRESHOLD`` (re-read at call time so an existing monkeypatch
    of the module constant still flips the default).
    """
    thr = COUNCIL_DRIFT_THRESHOLD if threshold is None else threshold
    n = len(claims)
    n_contradicted = sum(1 for c in claims if c["status"] == "contradicted")
    n_unverifiable = sum(1 for c in claims if c["status"] == "unverifiable")
    n_confirmed = n - n_contradicted - n_unverifiable
    denom = max(1, n)
    drift_ratio = (n_contradicted + n_unverifiable) / denom
    restart_ratio = n_contradicted / denom
    should_restart = n_contradicted > 0 and restart_ratio >= thr
    return {
        "n_claims": n,
        "n_confirmed": n_confirmed,
        "n_contradicted": n_contradicted,
        "n_unverifiable": n_unverifiable,
        "drift_ratio": drift_ratio,
        "restart_ratio": restart_ratio,
        "should_restart": should_restart,
    }


def _build_reseed_context(state: dict, research: str, drift: dict) -> str:
    """Splice the grounded-restart re-seed context (design §A2): OG topic +
    output-so-far + synth steer + grounding research.

    Re-run "round 1" seeded with this distinct grounded context so the panel
    re-deliberates over a corrected information set rather than starting fresh.
    """
    topic = state.get("task", "")
    handoff = state.get("council_handoff") or {}
    # Output-so-far: when grounding is ON, the synthesized answer is deferred to
    # council_pending_answer (NOT in messages on the restart path); prefer it,
    # falling back to the last synthesized assistant message (synth-failure turn).
    output_so_far = (state.get("council_pending_answer") or {}).get("content") or ""
    if not output_so_far:
        for m in reversed(state.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content"):
                output_so_far = m["content"]
                break
    # Synth steer = the unresolved debate + next-task directive the synth emitted.
    steer_bits = []
    if handoff.get("active_debate"):
        steer_bits.append("ACTIVE DEBATE: " + ", ".join(handoff["active_debate"]))
    if handoff.get("next_task"):
        steer_bits.append("NEXT TASK: " + str(handoff["next_task"]))
    steer = "\n".join(steer_bits) or "(none)"

    parts = [
        "GROUNDED RESTART — the prior round's answer drifted from the live web; "
        "re-deliberate over this corrected information set.",
        f"ORIGINAL TOPIC:\n{topic}",
        f"OUTPUT SO FAR (prior council answer — do NOT simply restate it):\n{output_so_far or '(none)'}",
        f"SYNTH STEER (unresolved + directive):\n{steer}",
        f"GROUNDING RESEARCH (live-web findings; {drift['n_contradicted']} contradicted, "
        f"{drift['n_unverifiable']} unverifiable of {drift['n_claims']} load-bearing claims):\n"
        f"{research or '(no research text returned)'}",
    ]
    return "\n\n".join(parts)


async def _converge(
    state: dict,
    *,
    verdict: Optional[dict],
    cost_usd: float,
    flagged: Optional[list[str]] = None,
    error: Optional[str] = None,
) -> dict:
    """Return the converge delta: stop here, keep the best handoff so far, and
    COMMIT the deferred final answer exactly once (P2).

    When grounding is ON, synthesize_node deferred the client emit + messages
    append onto ``council_pending_answer``. On EVERY converge path we commit it
    once here (writer "custom" chunk + L0 event via emit_synthesis, plus the
    assistant ``messages`` fragment) and clear the carrier. An intermediate
    (restart) round never reaches _converge, so it never commits. If no pending
    answer is present (already committed/cleared, e.g. a synth-failure turn) we
    commit nothing.

    Optionally appends ``flagged`` unverifiable claims to the handoff's
    ``active_debate`` so the human sees them (§A2 — unverifiable flags the
    handoff, doesn't restart). ``error`` (the cost-ceiling notice, Refinement 1)
    rides ``out["error"]`` which api/server.py relays as a single error frame —
    so the ceiling notice surfaces ONCE there, never via the custom channel.

    ``_route_after_ground`` routes to END iff ``council_spec["reseed_context"]``
    is absent, so a converge MUST clear any stale reseed context left by a
    *prior* restart round (otherwise a later converge would loop back to
    panel_dispatch). We only re-emit council_spec when it actually carried a
    stale reseed_context, so a clean first-pass converge leaves council_spec
    untouched (additive — no spurious state write).
    """
    out: dict = {
        "grounding_verdict": verdict,
        "council_cost_usd": cost_usd,
    }
    if error is not None:
        out["error"] = error
    pending = state.get("council_pending_answer") or {}
    if pending.get("content"):
        from core.nodes.synthesize import emit_synthesis
        out["messages"] = await emit_synthesis(
            state, pending["content"], pending.get("backend")
        )
        out["council_pending_answer"] = None
    spec = state.get("council_spec") or {}
    if spec.get("reseed_context"):
        cleared = dict(spec)
        cleared.pop("reseed_context", None)
        out["council_spec"] = cleared
    if flagged:
        handoff = dict(state.get("council_handoff") or {})
        debate = list(handoff.get("active_debate") or [])
        debate.extend(flagged)
        handoff["active_debate"] = debate
        out["council_handoff"] = handoff
    return out


async def grounding_node(state: dict) -> dict:
    """Pre-close grounding gate: verify the answer vs the live web, decide
    converge-vs-restart (fail-open on any grounding failure).

    Returns a state delta. For a CONVERGE it leaves ``council_restart``
    unchanged (``_route_after_ground`` → END). For a RESTART it increments
    ``council_restart`` and writes ``council_spec["reseed_context"]``
    (``_route_after_ground`` → ``panel_dispatch``). On a ceiling breach it routes
    through ``_converge`` with ``error=`` set (the notice rides the error frame)
    and converges (END).
    """
    spec = dict(state.get("council_spec") or {})
    bounds = spec.get("bounds") or {}
    topic = state.get("task", "")
    restart_count = state.get("council_restart") or 0
    prior_cost = state.get("council_cost_usd") or 0.0

    # ── Per-run grounding on/off (P3b) ───────────────────────────────────────
    # A profile's protocol_bounds.grounding overrides the global cadence. When
    # OFF for this run, synthesize already committed the answer IN-NODE, so this
    # node must be a pure no-op converge: no spawn, no second commit (_converge
    # finds council_pending_answer None and emits nothing). MUST read the SAME
    # grounding_enabled(spec) synthesize_node reads, or the answer double-emits/
    # drops. The graph now ALWAYS wires synthesize → ground, so this runtime
    # gate (not the old build-time topology gate) is what disables grounding.
    if not grounding_enabled(spec):
        logger.debug("council grounding disabled for this run (bounds/cadence); "
                     "no-op converge")
        return await _converge(state, verdict=None, cost_usd=prior_cost)

    # Per-run protocol bounds (a profile only overrides what it sets; unset keys
    # fall back to the global module constants — byte-identical to pre-P3b).
    max_usd = _bound_float(bounds.get("max_usd"), COUNCIL_MAX_USD)
    restart_budget = _bound_int(bounds.get("restart_budget"), COUNCIL_RESTART_BUDGET)
    drift_threshold = _bound_float(bounds.get("drift_threshold"), COUNCIL_DRIFT_THRESHOLD)

    # Find the synthesized answer to verify. When grounding is ON, synthesize
    # deferred the answer to council_pending_answer (it is NOT in messages yet);
    # fall back to the last assistant message (grounding-OFF can't reach here, but
    # the synth-failure turn leaves its error as the last assistant message).
    answer = (state.get("council_pending_answer") or {}).get("content") or ""
    if not answer:
        for m in reversed(state.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content"):
                answer = m["content"]
                break

    # ── Global cost ceiling (§A3) — fail LOUD before spending another spawn. ──
    # We are about to spend one grounding spawn; if even projecting it would
    # breach, OR we have already breached, fail loud to the human with the best
    # handoff so far. (Checked BEFORE the spawn so the breach itself is bounded.)
    projected_cost = prior_cost + GROUNDING_SPAWN_USD
    if prior_cost >= max_usd or projected_cost > max_usd:
        # Distinguish a genuine mid-run breach (already spent up to the ceiling)
        # from a MISCONFIGURED ceiling that can't even fund one grounding spawn
        # (max_usd < GROUNDING_SPAWN_USD): the latter means grounding NEVER ran
        # for this profile despite grounding=on, which otherwise reads identically
        # to a real breach (P3b review, finding #1 observability).
        hint = (
            (f" (the per-run ceiling ${max_usd:.2f} is below the cost of one "
             f"grounding spawn ${GROUNDING_SPAWN_USD:.2f}, so grounding could not "
             "run for this profile — raise max_usd or set grounding off)")
            if max_usd < GROUNDING_SPAWN_USD else ""
        )
        msg = (
            f"Council cost ceiling reached (${prior_cost:.2f} spent, ceiling "
            f"${max_usd:.2f}); returning the best handoff so far without "
            f"grounding. Review and steer if needed.{hint}"
        )
        logger.warning("council grounding ceiling breach: %s", msg)
        return await _converge(state, verdict=None, cost_usd=prior_cost, error=msg)

    if not answer:
        # Nothing to verify (defensive) — converge, no spend.
        logger.debug("council grounding: no answer to verify; converging")
        return await _converge(state, verdict=None, cost_usd=prior_cost)

    # ── Spawn the read-only WebSearch verifier (claude_code, direct). ─────────
    # NOT via _send_to_backend — claude_code is a subprocess called through
    # ClaudeCodeClient directly (mirrors execute_node's posture→argv mapping).
    if COUNCIL_GROUND_BACKEND != "claude_code":
        # Only claude_code is wired in P2 (Gemini second-verifier deferred).
        logger.warning(
            "council grounding backend %r not wired in P2; converging "
            "(fail-open)", COUNCIL_GROUND_BACKEND,
        )
        return await _converge(state, verdict=None, cost_usd=prior_cost)

    prompt = _VERIFY_PROMPT_TEMPLATE.format(topic=topic, answer=answer)
    cost_after = projected_cost  # the spawn is now attributed regardless of outcome

    try:
        from core.backends.claude_code import ClaudeCodeClient

        client = ClaudeCodeClient(
            cwd=config.CC_PROJECT_DIR,
            posture=GROUNDING_POSTURE,
            conversation_id=(state.get("conversation_id") or None),
        )
        result = await asyncio.wait_for(
            client.chat(prompt=prompt, posture=GROUNDING_POSTURE),
            timeout=config.CC_TIMEOUT_SECONDS,
        )
        raw = result.get("text") or ""
    except Exception as exc:  # noqa: BLE001 — ANY grounding failure fails OPEN.
        logger.warning(
            "council grounding spawn failed (%s: %s); failing open → converge",
            type(exc).__name__, exc,
        )
        # The spawn failed; do NOT attribute its full cost (it produced nothing).
        return await _converge(state, verdict=None, cost_usd=prior_cost)

    data = extract_json(raw)
    if data is None:
        logger.warning(
            "council grounding verdict unparseable; failing open → converge"
        )
        return await _converge(state, verdict={"raw": raw, "parse_error": True}, cost_usd=cost_after)

    claims, research = _normalize_claims(data)
    drift = compute_drift(claims, drift_threshold)
    verdict = {"claims": claims, "research": research, "drift": drift}
    logger.info(
        "council grounding: %d claims (%d confirmed / %d contradicted / %d "
        "unverifiable), restart_ratio=%.2f thresh=%.2f → %s",
        drift["n_claims"], drift["n_confirmed"], drift["n_contradicted"],
        drift["n_unverifiable"], drift["restart_ratio"], drift_threshold,
        "RESTART" if drift["should_restart"] else "converge",
    )

    # Unverifiable claims always flag the handoff (whether we restart or not).
    flagged = [
        f"[UNVERIFIED] {c['claim']}" for c in claims if c["status"] == "unverifiable" and c["claim"]
    ]

    if not drift["should_restart"]:
        # Converge — below threshold (or all-unverifiable / all-confirmed).
        return await _converge(state, verdict=verdict, cost_usd=cost_after, flagged=flagged)

    # ── Drift triggers a restart — but only within the restart budget. ────────
    if restart_count >= restart_budget:
        logger.info(
            "council grounding: drift detected but restart budget spent "
            "(%d/%d); converging with best handoff",
            restart_count, restart_budget,
        )
        # Budget exhausted → converge; flag the residual drift for the human.
        residual = flagged + [
            f"[CONTRADICTED] {c['claim']}" for c in claims if c["status"] == "contradicted" and c["claim"]
        ]
        return await _converge(state, verdict=verdict, cost_usd=cost_after, flagged=residual)

    # Grounded restart: re-seed round 1.
    reseed = _build_reseed_context(state, research, drift)
    spec["reseed_context"] = reseed
    # Clear the prior round's resolved seats / panel task so panel_dispatch
    # rebuilds them with the re-seed context spliced in.
    spec.pop("resolved_seats", None)
    spec.pop("panel_task", None)
    logger.info(
        "council grounding: grounded restart %d → re-seeding panel round 1",
        restart_count + 1,
    )
    return {
        "grounding_verdict": verdict,
        "council_cost_usd": cost_after,
        "council_spec": spec,
        "council_restart": restart_count + 1,
        # NOTE: panel_results is NOT reset here. Its reducer is operator.add, so
        # returning [] would append nothing (it can't clear). Instead the re-run
        # round stamps a higher ``round`` on its entries (panel: panel_round =
        # council_restart) and synthesize_node reads only the max-round entries,
        # so the re-seeded round cleanly supersedes the drifted one.
    }
