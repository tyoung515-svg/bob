"""MS2-R5 — refute-and-vote convergence + adversarial cross-check TERMINATION (the research lane's close).

A **refute-and-vote round controller**: an independent, **decorrelated** rollout attempts to **refute** the
prior round's surviving claims; only claims that survive the adversarial cross-check **AND** pass the
CitationAgent (R4) surface remain. Convergence is **DETERMINISTIC** — the surviving-claim set stabilizes
(no-delta across a round, the ``core/nodes/debate.py`` ``debate_converge_node`` **Idea-ID** pattern, keyed by
``bid_key``) OR ``max_rounds`` / ``max_usd`` bind. **Default-FAIL:** unsurvived (refuted) / unverified claims
fall to the **could-not-verify** set WITH reasons — never silently to the report.

This **REUSES the DETERMINISTIC convergence PATTERN of ``core/nodes/debate.py`` WITHOUT editing or importing
it** (that node is council-state-bound); the no-delta close + the budget-before-next-round ceiling are MIRRORED
in a claim-set context. ``debate.py`` stays byte-identical. It CALLS the landed engines and adds **NO new
verification logic**:
  * OD#5 (deterministic surviving-claim-set stability + the ``max_rounds`` / ``max_usd`` bound; LLM-chair is v2).
  * OD#6 (3-family decorrelation: asserter ≠ entailment-critic ≠ refuter; a same-family fallback **fails
    CLOSED** — no silent same-family refuter). The ``'qwen'`` floor family is registered in ``FAMILY_BY_BACKEND``.
  * the R4 CitationAgent surface (``run_citation_agent`` -> a ``CitationReport``) is the verification authority;
    MS-3 ``termination`` (``is_complete`` / ``could_not_verify`` / ``termination_decision``) is the Default-FAIL
    merge gate; MS-2 ``postcondition`` decorrelation resolves the cross-family refuter.

Import-light: no network/git/Qdrant at import; the refuter ``send``, the ``cite`` surface, and the ``refute``
step are injected (the ``_send_to_backend`` + ``run_citation_agent`` imports are LAZY). No concrete model name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Sequence

from core.config import (
    RESEARCH_CONVERGE_MAX_ROUNDS,
    RESEARCH_CONVERGE_MAX_USD,
    RESEARCH_REFUTE_ROUND_USD,
)
from core.verify.entailment import Claim
from core.verify.termination import (
    Criterion,
    could_not_verify,
    is_complete,
    termination_decision,
)
from core.verify.postcondition import (
    DEFAULT_CRITIC_PREFERENCE,
    PostConditionError,
    decorrelated_critic_backend,
    family_of,
    is_decorrelated,
)

logger = logging.getLogger("bobclaw.research.converge")


# ── Exception ────────────────────────────────────────────────────────────────

class ResearchConvergeError(RuntimeError):
    """Config/reuse misuse: empty asserter_backend, max_rounds<=0, a non-positive max_usd/round_usd, a missing
    cite, a same-family refuter override, OR no decorrelated refuter candidate (fail CLOSED)."""


# ── A) Refuter resolution — the OD#6 3-family, fail-CLOSED decorrelation (REUSES MS-2 routing) ──

DEFAULT_REFUTER_PREFERENCE: tuple[str, ...] = DEFAULT_CRITIC_PREFERENCE
"""The candidate pool for the refuter — the same six-family, cheap-first ordering as the MS-2 critic pool."""


def decorrelated_refuter_backend(
    asserter_backend: str,
    *,
    critic_backend: Optional[str] = None,
    team: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
) -> str:
    """Resolve a refuter cross-family from the ASSERTER (mandatory) AND — when ``critic_backend`` is given — the
    entailment CRITIC (the 3-family asserter≠critic≠refuter posture). Builds the pool the SAME way postcondition
    does (team critic -> candidates -> ``DEFAULT_REFUTER_PREFERENCE``, reusing ``core.teams.role_backend``),
    returns the FIRST candidate cross-family from BOTH; if none is cross-family from both, DELEGATES to
    ``postcondition.decorrelated_critic_backend`` (which GUARANTEES cross-family-from-asserter or raises) so the
    ≠asserter guarantee is never re-implemented. FAILS CLOSED (``ResearchConvergeError``) when NO candidate is
    cross-family from the asserter — NEVER a silent same-family refuter. PURE (only a lazy teams import)."""
    asserter_family = family_of(asserter_backend)
    critic_family = family_of(critic_backend) if critic_backend else None

    pool: list[str] = []
    if team is not None:
        from core.teams import role_backend  # lazy: keep the module import graph light

        team_critic = role_backend(team, "critic")
        if team_critic:
            pool.append(team_critic)
    if candidates:
        pool.extend(candidates)
    pool.extend(DEFAULT_REFUTER_PREFERENCE)

    # 1) FIRST choice — cross-family from BOTH asserter AND critic (the 3-family posture).
    for c in pool:
        if c:
            fam = family_of(c)
            if fam != asserter_family and (critic_family is None or fam != critic_family):
                return c

    # 2) FALLBACK — cross-family from the asserter ONLY (still decorrelated; shares the critic family).
    #    Delegate to postcondition so the ≠asserter guarantee (+ its fail-closed) is not re-implemented.
    try:
        return decorrelated_critic_backend(asserter_backend, team=team, candidates=candidates)
    except PostConditionError as exc:
        raise ResearchConvergeError(
            f"No decorrelated refuter candidate for asserter_backend={asserter_backend!r} "
            f"(family {asserter_family!r}); critic_family={critic_family!r}; pool={pool!r}"
        ) from exc


# ── B) The adversarial refuter step (one decorrelated cross-check per claim) ────

REFUTE_PROMPT_TEMPLATE: str = (
    "You are an ADVERSARIAL refuter in a research cross-check. Given a CLAIM (a specific number) and the "
    "EVIDENCE the asserting agent offered, try to REFUTE the number (wrong / unsupported / out-of-scope / "
    "stale / wrong-entity). Do NOT uphold merely because it sounds plausible. "
    'Answer "refuted" ONLY with a concrete reason the number is wrong or unsupported; "upheld" if the evidence '
    'genuinely supports the specific number and you cannot refute it; "unknown" if you cannot decide. '
    "Respond with a SINGLE line of JSON and nothing else:\n"
    '{"verdict":"refuted"|"upheld"|"unknown","reasons":["short reason", "..."]}\n'
    "Claim: {claim_text}\n"
    "Number: {numeric_value}\n"
    "Evidence: {evidence}"
)


def _safe_format(template: str, **kwargs: Any) -> str:
    """BRACE-SAFE single-pass substitution: a brace inside a substituted value is never rescanned (a claim /
    evidence with literal ``{ }`` cannot inject a later placeholder). Mirrors ``entailment._safe_format``."""

    def _repl(match: "re.Match") -> str:
        key = match.group(1)
        return str(kwargs[key]) if key in kwargs else match.group(0)

    return re.sub(r"\{(\w+)\}", _repl, template)


def build_refute_prompt(claim_text: str, numeric_value: object, evidence: str) -> str:
    """Render the adversarial refuter prompt (BRACE-SAFE) with the claim text, the numeric value, the evidence."""
    return _safe_format(
        REFUTE_PROMPT_TEMPLATE, claim_text=claim_text, numeric_value=str(numeric_value), evidence=evidence
    )


class RefuteVerdict(str, Enum):
    REFUTED = "refuted"  # the refuter AFFIRMATIVELY knocks the claim out (a concrete reason the number is wrong)
    UPHELD = "upheld"    # the evidence genuinely supports the specific number; cannot refute
    UNKNOWN = "unknown"  # cannot decide / unparseable / unreachable -> NOT a refutation (the adversary failed)


def parse_refute_verdict(raw: str) -> tuple[RefuteVerdict, list[str]]:
    """PURE, tolerant, STRING-LITERAL-aware. Strip a leading/trailing markdown fence; scan for ALL balanced
    top-level ``{...}`` substrings (a brace inside a JSON string never changes depth — track ``in_string`` +
    ``escape``, exactly like ``entailment.parse_entailment_verdict``); the LAST dict carrying ``"verdict"`` wins.
    Unparseable / no verdict -> ``(UNKNOWN, ["parse_error: ..."])``. NEVER raises."""
    if not raw:
        return (RefuteVerdict.UNKNOWN, ["parse_error: empty input"])

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text, count=1)
        text = re.sub(r"\n?```\s*$", "", text, count=1)

    # Collect every balanced top-level {...} substring, quote/escape-aware.
    objects: list[str] = []
    depth = 0
    start: Optional[int] = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : i + 1])
                    start = None

    # The LAST verdict-bearing dict wins (the model's actual answer is the trailing object).
    for obj_str in reversed(objects):
        try:
            data = json.loads(obj_str)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or "verdict" not in data:
            continue
        verdict_str = str(data.get("verdict", "")).strip().lower()
        try:
            verdict = RefuteVerdict(verdict_str)
        except ValueError:
            verdict = RefuteVerdict.UNKNOWN
        reasons_raw = data.get("reasons")
        if isinstance(reasons_raw, list):
            reasons = [str(r) for r in reasons_raw]
        elif reasons_raw is not None:
            reasons = [str(reasons_raw)]
        else:
            reasons = []
        return (verdict, reasons)

    return (RefuteVerdict.UNKNOWN, [f"parse_error: no verdict in response: {raw[:200]!r}"])


@dataclass(frozen=True)
class RefutationResult:
    """One claim's adversarial-refuter outcome."""
    bid_key: str
    verdict: RefuteVerdict
    refuted: bool           # verdict is RefuteVerdict.REFUTED
    reasons: tuple
    refuter_backend: str
    decorrelated: bool      # is_decorrelated(asserter_backend, refuter_backend)

    def as_dict(self) -> dict:
        return {
            "bid_key": self.bid_key,
            "verdict": self.verdict.value,
            "refuted": self.refuted,
            "reasons": list(self.reasons),
            "refuter_backend": self.refuter_backend,
            "decorrelated": self.decorrelated,
        }


async def _default_send(messages: list[dict], backend: str) -> str:
    """Lazy real-backend send (kept out of module import so this file stays import-light)."""
    from core.nodes.execute import _send_to_backend

    return await _send_to_backend(messages, backend)


async def refute_claim(
    *,
    claim: Claim,
    evidence: str = "",
    asserter_backend: str,
    refuter_backend: str,
    team: Optional[str] = None,
    send: Optional[Callable[[list[dict], str], Awaitable[str]]] = None,
) -> RefutationResult:
    """ONE decorrelated adversarial refuter step. ENFORCES cross-family: a refuter SAME family as the asserter
    raises ``ResearchConvergeError`` (fail CLOSED — no silent same-family cross-check). FAIL-SAFE: an unreachable
    / cancelled refuter, or an UNKNOWN/unparseable verdict, is NOT a refutation (verdict UNKNOWN, refuted=False)
    — an adversary must AFFIRMATIVELY refute to knock a claim out; the CitationAgent (Default-FAIL) remains the
    verification authority."""
    if not is_decorrelated(asserter_backend, refuter_backend):
        raise ResearchConvergeError(
            f"refuter_backend {refuter_backend!r} (family {family_of(refuter_backend)!r}) is SAME family as "
            f"asserter_backend {asserter_backend!r} (family {family_of(asserter_backend)!r}) — fail CLOSED"
        )

    decorrelated = is_decorrelated(asserter_backend, refuter_backend)
    prompt = build_refute_prompt(claim.render(), claim.numeric_value, evidence)
    messages = [{"role": "user", "content": prompt}]
    send_fn = send or _default_send

    try:
        raw = await send_fn(messages, refuter_backend)
    except asyncio.CancelledError:  # noqa: BLE001 — cancellation is NOT a refutation
        return RefutationResult(
            bid_key=claim.bid_key, verdict=RefuteVerdict.UNKNOWN, refuted=False,
            reasons=("refuter verification cancelled",), refuter_backend=refuter_backend, decorrelated=decorrelated,
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe: an unreachable adversary is NOT a refutation
        logger.warning("refuter call failed (backend=%r, claim=%s): %s", refuter_backend, claim.bid_key, exc)
        return RefutationResult(
            bid_key=claim.bid_key, verdict=RefuteVerdict.UNKNOWN, refuted=False,
            reasons=(f"refuter_unavailable: {type(exc).__name__}: {exc}",),
            refuter_backend=refuter_backend, decorrelated=decorrelated,
        )

    verdict, reasons = parse_refute_verdict(raw)
    return RefutationResult(
        bid_key=claim.bid_key, verdict=verdict, refuted=(verdict is RefuteVerdict.REFUTED),
        reasons=tuple(reasons), refuter_backend=refuter_backend, decorrelated=decorrelated,
    )


# ── C) The refute-and-vote round controller (the deterministic close) ──────────

@dataclass(frozen=True)
class RoundResult:
    """One refute-and-vote round: what entered, what survived, what was filtered, and the round's citation evidence."""
    round_idx: int
    survivors_before: tuple      # bid_keys entering the round
    survivors_after: tuple       # bid_keys surviving (citation-VERIFIED AND NOT refuter-REFUTED)
    refuted: tuple               # bid_keys the refuter AFFIRMATIVELY knocked out this round
    unverified: tuple            # bid_keys the CitationAgent surface did NOT verify this round
    refutations: tuple           # RefutationResult per survivor-before
    citation: dict               # the R4 CitationReport.as_dict() for the round (durable evidence)
    cost_usd: float

    def as_dict(self) -> dict:
        return {
            "round_idx": self.round_idx,
            "survivors_before": list(self.survivors_before),
            "survivors_after": list(self.survivors_after),
            "refuted": list(self.refuted),
            "unverified": list(self.unverified),
            "refutations": [r.as_dict() for r in self.refutations],
            "citation": self.citation,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class ConvergeResult:
    """The refute-and-vote controller's assembled output: robust survivors + the Default-FAIL could-not-verify set."""
    surviving: tuple                    # tuple[Claim] — the robust survivors (final)
    surviving_keys: tuple               # their bid_keys
    could_not_verify: tuple             # tuple[dict] {"bid_key","kind":"refuted"|"unverified","reasons","round"}
    converged: bool                     # ALWAYS True (a deterministic controller always terminates)
    converged_reason: str
    rounds: int
    round_results: tuple
    cost_usd: float
    budget_bound: bool                  # convergence fired on the max_usd ceiling
    asserter_backend: str
    refuter_backend: str
    critic_backend: Optional[str]
    decorrelated: bool                  # is_decorrelated(asserter, refuter) — ALWAYS True
    three_family: bool                  # asserter, critic, refuter families ALL pairwise distinct (False when critic unknown)
    complete: bool                      # is_complete(surviving criteria) — the Default-FAIL merge gate
    decision: dict                      # termination_decision(surviving criteria) — FAST_FORWARD/REVERT/ESCALATE

    def as_dict(self) -> dict:
        return {
            "surviving": [{"bid_key": c.bid_key, "text": c.render()} for c in self.surviving],
            "surviving_keys": list(self.surviving_keys),
            "could_not_verify": list(self.could_not_verify),
            "converged": self.converged,
            "converged_reason": self.converged_reason,
            "rounds": self.rounds,
            "round_results": [rr.as_dict() for rr in self.round_results],
            "cost_usd": self.cost_usd,
            "budget_bound": self.budget_bound,
            "asserter_backend": self.asserter_backend,
            "refuter_backend": self.refuter_backend,
            "critic_backend": self.critic_backend,
            "decorrelated": self.decorrelated,
            "three_family": self.three_family,
            "complete": self.complete,
            "decision": self.decision,
        }


CiteCallable = Callable[[Sequence[Claim]], Awaitable[Any]]  # -> a CitationReport-like (.results / .could_not_verify)


def _verified_keys(report: Any) -> set:
    """The set of ``bid_key`` the round's CitationReport marked verified (``r.verified is True``)."""
    return {r.bid_key for r in getattr(report, "results", []) if getattr(r, "verified", False)}


def _cnv_reasons(report: Any) -> dict:
    """``{bid_key: reasons}`` from the CitationReport's could-not-verify list (the unverified claims' reasons)."""
    cnv = getattr(report, "could_not_verify", None) or []
    return {d.get("bid_key"): d.get("reasons") for d in cnv}


def _dedup_by_bidkey(claims: Sequence[Claim]) -> list:
    """First ``Claim`` per ``bid_key`` wins, INPUT ORDER preserved."""
    seen: set = set()
    out: list = []
    for c in claims:
        k = c.bid_key
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


async def _run_round(
    round_idx: int,
    survivors: list,
    *,
    cite: CiteCallable,
    refute_fn: Callable[..., Awaitable[RefutationResult]],
    ev_for: Callable[[Claim], str],
    asserter_backend: str,
    refuter_backend: str,
    team: Optional[str],
    refuter_send: Optional[Callable[..., Awaitable[str]]],
    round_usd: float,
    concurrency: int,
) -> "tuple[RoundResult, list, dict]":
    """Run one round: the CitationAgent surface over the current survivors, then the decorrelated refuter attacks
    each. A claim SURVIVES iff citation-VERIFIED AND NOT refuter-REFUTED. Returns
    ``(RoundResult, survivor_claims, dropped)`` where ``dropped`` maps ``bid_key -> {kind, reasons}``."""
    report = await cite(list(survivors))
    verified = _verified_keys(report)
    cnv_reasons = _cnv_reasons(report)

    sem = asyncio.Semaphore(concurrency)

    async def _one(c: Claim) -> "tuple[Claim, RefutationResult]":
        async with sem:
            return c, await refute_fn(
                claim=c, evidence=ev_for(c), asserter_backend=asserter_backend,
                refuter_backend=refuter_backend, team=team, send=refuter_send,
            )

    pairs = await asyncio.gather(*[_one(c) for c in survivors])  # INPUT ORDER preserved by gather

    refutations: list = []
    survivor_claims: list = []
    refuted: list = []
    unverified: list = []
    dropped: dict = {}
    for c, rr in pairs:
        refutations.append(rr)
        k = c.bid_key
        if rr.refuted:
            refuted.append(k)
            dropped[k] = {"kind": "refuted", "reasons": list(rr.reasons) or ["refuted"]}
        elif k not in verified:
            unverified.append(k)
            dropped[k] = {"kind": "unverified", "reasons": list(cnv_reasons.get(k) or ["not citation-verified"])}
        else:
            survivor_claims.append(c)

    round_result = RoundResult(
        round_idx=round_idx,
        survivors_before=tuple(c.bid_key for c in survivors),
        survivors_after=tuple(c.bid_key for c in survivor_claims),
        refuted=tuple(refuted),
        unverified=tuple(unverified),
        refutations=tuple(refutations),
        citation=report.as_dict() if hasattr(report, "as_dict") else {},
        cost_usd=round_usd,
    )
    return round_result, survivor_claims, dropped


async def run_refute_and_vote(
    *,
    claims: Sequence[Claim],
    cite: CiteCallable,
    asserter_backend: str,
    refuter_backend: Optional[str] = None,
    critic_backend: Optional[str] = None,
    team: Optional[str] = None,
    refuter_send: Optional[Callable[..., Awaitable[str]]] = None,
    evidence_for: Optional[Callable[[Claim], str]] = None,
    refute: Optional[Callable[..., Awaitable[RefutationResult]]] = None,
    max_rounds: int = RESEARCH_CONVERGE_MAX_ROUNDS,
    max_usd: float = RESEARCH_CONVERGE_MAX_USD,
    round_usd: float = RESEARCH_REFUTE_ROUND_USD,
    concurrency: int = 4,
) -> ConvergeResult:
    """The refute-and-vote round controller (DETERMINISTIC convergence).

    Deduplicate claims by ``bid_key``. Resolve the refuter cross-family (fail CLOSED). Each round: (1) run the
    CitationAgent surface over the CURRENT survivors -> the verified set; (2) the decorrelated refuter attacks
    each survivor (seeing ``evidence_for(claim)``); (3) a claim SURVIVES iff citation-VERIFIED AND NOT
    refuter-REFUTED — refuted -> dropped (kind ``"refuted"``), citation-unverified -> dropped (kind
    ``"unverified"``), both surfaced in ``could_not_verify`` WITH reasons (Default-FAIL). Convergence (OD#5, the
    ``debate_converge`` no-delta Idea-ID pattern by ``bid_key``): converge when the surviving set is STABLE
    (no-delta), or empty, or the round cap, or the ``max_usd`` ceiling binds BEFORE spending another round.
    Reuses the ``debate.py`` pattern WITHOUT editing it; ``debate.py`` stays byte-identical."""
    if not (isinstance(asserter_backend, str) and asserter_backend.strip()):
        raise ResearchConvergeError("asserter_backend is required (non-empty string)")
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds <= 0:
        raise ResearchConvergeError("max_rounds must be a positive int")
    if not isinstance(max_usd, (int, float)) or isinstance(max_usd, bool) or max_usd <= 0:
        raise ResearchConvergeError("max_usd must be positive")
    if not isinstance(round_usd, (int, float)) or isinstance(round_usd, bool) or round_usd <= 0:
        raise ResearchConvergeError("round_usd must be positive")
    if cite is None:
        raise ResearchConvergeError("cite callable is required")

    # Resolve the refuter cross-family (fail CLOSED). An explicit same-family override fails CLOSED too.
    if refuter_backend is not None:
        rb = refuter_backend
        if not is_decorrelated(asserter_backend, rb):
            raise ResearchConvergeError(
                f"explicit refuter_backend {rb!r} is SAME family as asserter {asserter_backend!r} — fail CLOSED"
            )
    else:
        rb = decorrelated_refuter_backend(asserter_backend, critic_backend=critic_backend, team=team)

    refute_fn = refute or refute_claim
    ev_for = evidence_for or (lambda c: c.render())

    survivors = _dedup_by_bidkey(claims)
    all_seen = {c.bid_key: c for c in survivors}  # every claim ever entered (for the Default-FAIL reconcile)
    cost = 0.0
    rounds_run = 0
    round_results: list = []
    cnv_items: list = []  # {bid_key, kind, reasons, round}
    converged_reason = ""
    budget_bound = False

    # ── the deterministic convergence loop (MIRRORS debate_converge_node, keyed by bid_key) ──
    while rounds_run < max_rounds:
        prev_keys = {c.bid_key for c in survivors}
        if not prev_keys:
            converged_reason = "empty surviving set"
            break
        # budget: fail loud BEFORE spending another round (debate parity — return the best-so-far survivors).
        if cost + round_usd > max_usd:
            converged_reason = f"budget ceiling ${max_usd:.2f}"
            budget_bound = True
            break

        round_result, survivor_claims, dropped = await _run_round(
            rounds_run, survivors, cite=cite, refute_fn=refute_fn, ev_for=ev_for,
            asserter_backend=asserter_backend, refuter_backend=rb, team=team,
            refuter_send=refuter_send, round_usd=round_usd, concurrency=concurrency,
        )
        cost += round_usd
        rounds_run += 1
        round_results.append(round_result)
        for bk, info in dropped.items():
            cnv_items.append({"bid_key": bk, "kind": info["kind"], "reasons": info["reasons"], "round": rounds_run - 1})

        survivors = survivor_claims
        cur_keys = {c.bid_key for c in survivors}
        if cur_keys == prev_keys:
            converged_reason = "no-delta round (surviving claim-set stable)"
            break
        if not cur_keys:
            converged_reason = "empty surviving set"
            break
    else:
        converged_reason = f"round cap {max_rounds}"

    # ── Default-FAIL: a claim counts as VERIFIED only if it survived at least one REAL round (citation-verified
    # AND refuter-upheld). If the budget ceiling binds BEFORE any round runs (rounds_run == 0), the "best-so-far"
    # survivors were NEVER checked — they must NOT be marked verified. Route them to could_not_verify (the
    # contested-by-cost surface, §2.7) so a zero-round budget bind can never FAST_FORWARD an unverified claim.
    if budget_bound and rounds_run == 0 and survivors:
        for c in survivors:
            cnv_items.append({"bid_key": c.bid_key, "kind": "unverified",
                              "reasons": ["budget ceiling bound before any verification round"], "round": 0})
        survivors = []

    # ── the Default-FAIL merge gate over the SURVIVORS (MS-3 termination reuse) ──
    # A budget bind is CONTESTED-BY-COST (§2.7): the adversarial convergence did not complete, so the run
    # ESCALATEs (surfaced, never auto-FAST_FORWARDed) — mergegate's ``budget_escalated`` path.
    surviving_keys = [c.bid_key for c in survivors]
    surviving_set = set(surviving_keys)
    surviving_criteria = [Criterion(key=k, verified=True) for k in surviving_keys]
    complete = is_complete(surviving_criteria, budget_escalated=budget_bound)
    decision = termination_decision(surviving_criteria, budget_escalated=budget_bound)

    # Default-FAIL reconcile: termination.could_not_verify over the FULL (survivors verified + dropped
    # unverified) criteria set is the CANONICAL not-verified key set; ensure every such key is SURFACED (never
    # silently dropped) — cnv_items already enriches it with kind/reasons/round; add a defensive fallback entry
    # for any canonical could-not-verify key the round loop somehow missed.
    all_criteria = [Criterion(key=k, verified=(k in surviving_set)) for k in all_seen]
    canonical_cnv = {c.key for c in could_not_verify(all_criteria)}
    surfaced = {it["bid_key"] for it in cnv_items}
    for k in all_seen:
        if k in canonical_cnv and k not in surfaced:
            cnv_items.append({"bid_key": k, "kind": "unverified",
                              "reasons": ["not verified (default-fail)"], "round": max(rounds_run - 1, 0)})

    three_family = (
        critic_backend is not None
        and len({family_of(asserter_backend), family_of(critic_backend), family_of(rb)}) == 3
    )

    return ConvergeResult(
        surviving=tuple(survivors),
        surviving_keys=tuple(surviving_keys),
        could_not_verify=tuple(cnv_items),
        converged=True,
        converged_reason=converged_reason,
        rounds=rounds_run,
        round_results=tuple(round_results),
        cost_usd=cost,
        budget_bound=budget_bound,
        asserter_backend=asserter_backend,
        refuter_backend=rb,
        critic_backend=critic_backend,
        decorrelated=is_decorrelated(asserter_backend, rb),
        three_family=three_family,
        complete=complete,
        decision=decision,
    )


def make_citation_check(
    *,
    retriever_for: Callable,
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable] = None,
    max_attempts: int = 8,
    concurrency: int = 4,
) -> CiteCallable:
    """Bind R4's ``run_citation_agent`` into a ``cite(claims) -> CitationReport`` callable for the controller
    (the "reuse the R4 CitationAgent surface" bridge). LAZY import of ``core.research.citation.run_citation_agent``."""

    async def _cite(claims: Sequence[Claim]) -> Any:
        from core.research.citation import run_citation_agent

        return await run_citation_agent(
            claims=list(claims), retriever_for=retriever_for, actor_backend=actor_backend, team=team,
            critic_backend=critic_backend, send=send, max_attempts=max_attempts, concurrency=concurrency,
        )

    return _cite


__all__ = [
    "ResearchConvergeError",
    "DEFAULT_REFUTER_PREFERENCE",
    "decorrelated_refuter_backend",
    "REFUTE_PROMPT_TEMPLATE",
    "build_refute_prompt",
    "RefuteVerdict",
    "parse_refute_verdict",
    "RefutationResult",
    "refute_claim",
    "RoundResult",
    "ConvergeResult",
    "CiteCallable",
    "run_refute_and_vote",
    "make_citation_check",
]
