"""MS2-R4 — the CitationAgent: claim entailment over MS-3, with a Default-FAIL publish gate.

The research lane's centerpiece differentiator — but a THIN productization over the LANDED MS-3 entailment
engine (``core/verify/entailment.py``): it adds **NO new verification logic**. It only plumbs around the
engine (DESIGN-MS-D2 §2.1):

1. **Claim extraction** (the ONE new model-touching piece, ~1 LLM step): pull quantitative claims from a
   synthesized draft into ``entailment.Claim`` objects. A RECALL problem (a missed claim never enters the
   gate — the silent failure Default-FAIL can't catch; R7 SES-measures extraction recall).
2. **Bind the ``retrieve`` callable:** ``run_entailment_gate(claim, retrieve=<R1 retriever>, ...)`` — bind the
   R1 retriever so the ERG's decorrelated re-branch pulls a *different* primary source. The firewall MS-3
   enforces (free-text reasons NEVER cross the re-branch — only the negative constraint + the typed enum cross)
   is PRESERVED here, never re-implemented.
3. **Run-the-gate-per-claim + tag + surface:** ``tag_for`` maps PV/VS/U; ``surface_could_not_verify`` returns
   the U/exhausted set WITH reasons.
4. **Default-FAIL publish:** each ``GateOutcome`` → ``criterion_from_outcome`` → a ``Criterion``; a claim
   publishes only when ``is_complete`` (verified OR exhausted-tagged). An exhausted claim is committed
   ``[UNVERIFIED: EXHAUSTED_SEARCH]`` and SURFACED, never silently dropped.
5. **Scoring (reuse):** ``make_entailment_verifier`` is already a valid ``core/ses/falsepass`` callable — wire
   the headline ``false_pass_rate`` on a planted wrong-but-plausible set.

Import-light: no network/git/Qdrant at import; the extractor model (``model_send``), the entailment critic
(``send``), and the retriever are injected (the ``_send_to_backend`` default is a LAZY import). No concrete
model name anywhere. The MS-3 engine is CONSUMED byte-identical — entailment/termination/postcondition are
never edited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from core.verify.entailment import (
    Claim,
    GateOutcome,
    RetrieveRequest,
    Source,
    VerificationTag,
    make_entailment_verifier,
    run_entailment_gate,
    surface_could_not_verify,
)
from core.verify.termination import (
    Criterion,
    criterion_from_outcome,
    is_complete,
    termination_decision,
)
from core.research.retrieve import make_research_retriever
from core.ses.falsepass import false_pass_rate

logger = logging.getLogger("bobclaw.research.citation")

# The callable types the gate / extractor consume (documentation aliases).
RetrieveCallable = Callable[[RetrieveRequest], Awaitable[Optional[Union[Source, Dict[str, Any]]]]]
SendCallable = Callable[[List[Dict[str, Any]], str], Awaitable[str]]


# ── Exception ────────────────────────────────────────────────────────────────

class CitationAgentError(RuntimeError):
    """Construction/config misuse: an empty actor_backend, or a non-positive concurrency/max_attempts.

    NOTE an EMPTY claim set is NOT an error — it is routed through the Default-FAIL REVERT path (no evidence
    ⇒ never a vacuous FAST_FORWARD); only the gate-driver config is validated here.
    """


# ── A) Claim extraction — the ONE new model-touching piece (a RECALL problem) ──

EXTRACTION_PROMPT_TEMPLATE: str = (
    "You are a precise claim extractor. From the research draft below, extract EVERY quantitative claim — "
    "any statement asserting a SPECIFIC NUMBER (a measurement, percentage, count, rate, score, size, price, "
    "or year). For each, identify the subject, the predicate (the relation), the numeric_value (the bare "
    "number as a string), and the cited_source_id when the draft attributes the number to a source (else "
    "null). Do NOT invent claims. Do NOT include qualitative statements (no number = not a claim). "
    "Respond with a SINGLE line of JSON and nothing else:\n"
    '{"claims":[{"subject":"...","predicate":"...","numeric_value":"...","cited_source_id":"..."|null,"text":"..."}]}\n'
    "Draft:\n{draft}"
)


def _safe_format(template: str, **kwargs: str) -> str:
    """BRACE-SAFE single-pass substitution: a brace inside a substituted value is never rescanned.

    Unlike sequential ``str.replace`` / ``str.format``, a draft containing literal ``{ }`` (JSON, code) cannot
    inject a later placeholder. Mirrors ``entailment._safe_format``.
    """

    def _repl(match: "re.Match") -> str:
        key = match.group(1)
        return kwargs.get(key, match.group(0))

    return re.sub(r"\{(\w+)\}", _repl, template)


def build_extraction_prompt(draft: str) -> str:
    """Render the quantitative-claim extraction prompt with BRACE-SAFE substitution."""
    return _safe_format(EXTRACTION_PROMPT_TEMPLATE, draft=draft)


def _last_json_object_with_key(text: str, key: str) -> Optional[dict]:
    """Return the LAST balanced top-level ``{...}`` object that parses to a dict carrying ``key``.

    STRING-LITERAL aware (a brace inside a JSON string never changes depth) and the model's ACTUAL answer is
    the FINAL object — an earlier prose-embedded object never wins. Strips a leading/trailing markdown fence.
    PURE; never raises.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        nl = cleaned.find("\n")
        if nl != -1:
            cleaned = cleaned[nl + 1:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    candidates: List[str] = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(cleaned):
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
                    candidates.append(cleaned[start: i + 1])
                    start = None
    result: Optional[dict] = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and key in obj:
            result = obj
    return result


def _coerce_claim(item: Any) -> Optional[Claim]:
    """Coerce a dict / Claim into a Claim, or None if it lacks a non-empty subject/predicate (or is malformed)."""
    if isinstance(item, Claim):
        claim = item
    elif isinstance(item, Mapping):
        try:
            claim = Claim.coerce(dict(item))
        except Exception:  # noqa: BLE001 — a malformed item is SKIPPED, never a crash (the never-raises contract)
            return None
    else:
        return None
    if not str(claim.subject or "").strip() or not str(claim.predicate or "").strip():
        return None
    return claim


def parse_extracted_claims(reply: str) -> List[Claim]:
    """PURE, tolerant. Extract the LAST balanced JSON object carrying ``claims`` → a list of ``Claim``.

    Coerces each item via ``Claim.coerce``; SKIPS any item missing a non-empty subject/predicate (a malformed
    extraction is dropped, NOT a crash). NEVER raises; no JSON / no ``claims`` list ⇒ ``[]``.
    """
    obj = _last_json_object_with_key(reply, "claims")
    if obj is None:
        return []
    raw = obj.get("claims")
    if not isinstance(raw, list):
        return []
    out: List[Claim] = []
    for item in raw:
        claim = _coerce_claim(item)
        if claim is not None:
            out.append(claim)
    return out


def claims_from_structured(claim_dicts: Iterable[Union[Mapping, Claim]]) -> List[Claim]:
    """PURE — the R3 dependency (subagent ``CondensedReturn.claims[]`` shape).

    Coerce R3's structured claim dicts ``{subject,predicate,numeric_value,cited_source_id,text}`` into ``Claim``
    objects; SKIP any missing subject/predicate. NEVER raises.
    """
    out: List[Claim] = []
    for item in claim_dicts or []:
        claim = _coerce_claim(item)
        if claim is not None:
            out.append(claim)
    return out


def _default_send(messages: List[Dict[str, Any]], backend: str) -> Awaitable[str]:
    """Lazy real-backend send (kept out of module import so this file stays import-light)."""
    from core.nodes.execute import _send_to_backend as _real_send

    return _real_send(messages, backend)


async def extract_claims(
    *,
    draft: str,
    backend: str,
    model_send: Optional[SendCallable] = None,
) -> List[Claim]:
    """ONE LLM step: send the extraction prompt to ``backend``, parse the reply → ``Claim`` objects.

    The model-touching extractor (a RECALL problem; R7 measures recall). An empty/whitespace draft ⇒ ``[]``
    WITHOUT a model call. A model error is the caller's concern (not swallowed) so a systemic extractor failure
    is visible — never a silent empty extraction.
    """
    if not (isinstance(draft, str) and draft.strip()):
        return []
    send = model_send or _default_send
    raw = await send([{"role": "user", "content": build_extraction_prompt(draft)}], backend)
    return parse_extracted_claims(raw)


# ── B) Per-claim gate driver + tag + Default-FAIL publish (THIN over MS-3) ──────

@dataclass(frozen=True)
class CitationResult:
    """One claim's citation outcome: the MS-3 ``GateOutcome`` + its PV/VS/U tag + the Default-FAIL publish flag."""
    claim: Claim
    bid_key: str
    tag: VerificationTag
    verified: bool
    exhausted: bool
    published: bool
    committed_tag: Optional[str]
    critic_backend: str
    decorrelated: bool
    reasons: Tuple[str, ...]
    outcome: GateOutcome
    criterion: Criterion

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bid_key": self.bid_key,
            "tag": self.tag.value,
            "verified": self.verified,
            "exhausted": self.exhausted,
            "published": self.published,
            "committed_tag": self.committed_tag,
            "critic_backend": self.critic_backend,
            "decorrelated": self.decorrelated,
            "reasons": list(self.reasons),
            "claim": {
                "subject": self.claim.subject,
                "predicate": self.claim.predicate,
                "numeric_value": self.claim.numeric_value,
                "cited_source_id": self.claim.cited_source_id,
            },
            "outcome": self.outcome.as_dict(),
        }


def _critic_of(outcome: GateOutcome) -> Tuple[str, bool]:
    """The LAST attempt's resolved critic backend + its decorrelation flag (the real entailment critic)."""
    critic = ""
    decorrelated = False
    for a in outcome.attempts:
        if a.critic_backend:
            critic = a.critic_backend
            decorrelated = a.decorrelated
    return critic, decorrelated


async def run_citation_gate(
    *,
    claim: Claim,
    retrieve: RetrieveCallable,
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[SendCallable] = None,
    max_attempts: int = 8,
) -> CitationResult:
    """THIN: call MS-3 ``run_entailment_gate``, then ``criterion_from_outcome`` → ``is_complete`` for the publish
    flag and ``outcome.final_tag`` for the PV/VS/U tag. Adds ZERO verification logic — the entailment, the ERG
    re-branch (+ its free-text firewall), and the decorrelation all live in MS-3; R4 only reads the result.
    """
    outcome = await run_entailment_gate(
        claim=claim,
        retrieve=retrieve,
        actor_backend=actor_backend,
        team=team,
        critic_backend=critic_backend,
        send=send,
        max_attempts=max_attempts,
    )
    criterion = criterion_from_outcome(outcome.as_dict())
    published = is_complete([criterion])
    critic, decorrelated = _critic_of(outcome)
    reasons = tuple(r for a in outcome.attempts for r in a.reasons)
    return CitationResult(
        claim=claim,
        bid_key=outcome.bid_key,
        tag=outcome.final_tag,
        verified=outcome.verified,
        exhausted=outcome.exhausted,
        published=published,
        committed_tag=outcome.committed_tag,
        critic_backend=critic,
        decorrelated=decorrelated,
        reasons=reasons,
        outcome=outcome,
        criterion=criterion,
    )


# ── C) The CitationAgent driver (assemble over a claim set) ────────────────────

@dataclass(frozen=True)
class CitationReport:
    """The CitationAgent's assembled output over a claim set: published + could-not-verify + the Default-FAIL gate."""
    results: Tuple[CitationResult, ...]
    published: Tuple[CitationResult, ...]
    could_not_verify: Tuple[Dict[str, Any], ...]
    complete: bool
    decision: Dict[str, Any]
    actor_backend: str
    counts: Dict[str, int]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "results": [r.as_dict() for r in self.results],
            "published": [r.as_dict() for r in self.published],
            "could_not_verify": list(self.could_not_verify),
            "complete": self.complete,
            "decision": self.decision,
            "actor_backend": self.actor_backend,
            "counts": dict(self.counts),
        }


def _validate_gate_config(actor_backend: str, concurrency: int, max_attempts: int) -> None:
    """Fail-fast validation of the gate-driver config (shared by run_citation_agent + cite_draft)."""
    if not (isinstance(actor_backend, str) and actor_backend.strip()):
        raise CitationAgentError("actor_backend is required")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        raise CitationAgentError("concurrency must be a positive int")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
        raise CitationAgentError("max_attempts must be a positive int")


def _count_results(results: Sequence[CitationResult]) -> Dict[str, int]:
    counts = {"PV": 0, "VS": 0, "U": 0, "verified": 0, "exhausted": 0, "published": 0, "total": len(results)}
    for r in results:
        counts[r.tag.value] = counts.get(r.tag.value, 0) + 1
        counts["verified"] += int(r.verified)
        counts["exhausted"] += int(r.exhausted)
        counts["published"] += int(r.published)
    return counts


async def run_citation_agent(
    *,
    claims: Sequence[Claim],
    retriever_for: Callable[[Claim], RetrieveCallable],
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[SendCallable] = None,
    max_attempts: int = 8,
    concurrency: int = 4,
) -> CitationReport:
    """For each claim: bind its R1 retriever via ``retriever_for(claim)``; run the gate; tag PV/VS/U; Default-FAIL
    the publish (``is_complete``). Assemble + ``surface_could_not_verify``. Bounded concurrency; results in INPUT
    ORDER. An EMPTY claim set ⇒ a REVERT report (Default-FAIL on no evidence — never a vacuous FAST_FORWARD).
    """
    _validate_gate_config(actor_backend, concurrency, max_attempts)

    claim_list = list(claims)
    sem = asyncio.Semaphore(concurrency)

    async def _one(c: Claim) -> CitationResult:
        async with sem:
            return await run_citation_gate(
                claim=c,
                retrieve=retriever_for(c),
                actor_backend=actor_backend,
                team=team,
                critic_backend=critic_backend,
                send=send,
                max_attempts=max_attempts,
            )

    results: List[CitationResult] = list(await asyncio.gather(*[_one(c) for c in claim_list]))
    criteria = [r.criterion for r in results]
    outcomes = [r.outcome for r in results]
    complete = is_complete(criteria)
    decision = termination_decision(criteria)
    published = tuple(r for r in results if r.published)
    could_not_verify = tuple(surface_could_not_verify(outcomes))
    counts = _count_results(results)
    return CitationReport(
        results=tuple(results),
        published=published,
        could_not_verify=could_not_verify,
        complete=complete,
        decision=decision,
        actor_backend=actor_backend,
        counts=counts,
    )


async def cite_draft(
    *,
    draft: str,
    retriever_for: Callable[[Claim], RetrieveCallable],
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    model_send: Optional[SendCallable] = None,
    extract_backend: Optional[str] = None,
    send: Optional[SendCallable] = None,
    max_attempts: int = 8,
    concurrency: int = 4,
) -> CitationReport:
    """The end-to-end CitationAgent over a free-text draft: ``extract_claims`` THEN ``run_citation_agent``.

    Validates the gate-driver config FAIL-FAST (before the extractor LLM call) so a misconfigured run never
    wastes a model call.
    """
    _validate_gate_config(actor_backend, concurrency, max_attempts)
    claims = await extract_claims(
        draft=draft,
        backend=extract_backend or actor_backend,
        model_send=model_send,
    )
    return await run_citation_agent(
        claims=claims,
        retriever_for=retriever_for,
        actor_backend=actor_backend,
        team=team,
        critic_backend=critic_backend,
        send=send,
        max_attempts=max_attempts,
        concurrency=concurrency,
    )


def lks_retriever_factory(
    *,
    lks_adapter: Any = None,
    lks_instances: Sequence[str] = (),
    web_tool: Any = None,
    query_for: Optional[Callable[[Claim], str]] = None,
    **retriever_kwargs: Any,
) -> Callable[[Claim], RetrieveCallable]:
    """Return ``retriever_for(claim)`` building an R1 ``ResearchRetriever`` bound to the claim's query.

    The query is ``query_for(claim)`` (default ``claim.render()``); the result is ``make_research_retriever(...)
    .retrieve`` — the exact MS-3 gate callable. This is the "bind the ``retrieve`` callable" step (§2.1 #2).
    PURE (no IO at factory build).
    """

    def _for(claim: Claim) -> RetrieveCallable:
        query = query_for(claim) if query_for is not None else claim.render()
        return make_research_retriever(
            query=query,
            lks_adapter=lks_adapter,
            lks_instances=tuple(lks_instances),
            web_tool=web_tool,
            **retriever_kwargs,
        )

    return _for


# ── D) Scoring — wire the headline metric (reuse: one line over MS-3 + MS-5) ───

def citation_false_pass_rate(
    planted_items: Iterable[Any],
    *,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[SendCallable] = None,
    default_actor_backend: Optional[str] = None,
) -> Dict[str, Any]:
    """``false_pass_rate(planted_items, make_entailment_verifier(...))`` — the lane's headline metric.

    ``make_entailment_verifier`` is ALREADY a valid falsepass callable (§2.1 #5); R4 only wires it. Pure
    measurement (the verifier sees only ``item.payload``, never the label).
    """
    verifier = make_entailment_verifier(
        team=team,
        critic_backend=critic_backend,
        send=send,
        default_actor_backend=default_actor_backend,
    )
    return false_pass_rate(planted_items, verifier)


__all__ = [
    "CitationAgentError",
    "CitationResult",
    "CitationReport",
    "EXTRACTION_PROMPT_TEMPLATE",
    "build_extraction_prompt",
    "parse_extracted_claims",
    "extract_claims",
    "claims_from_structured",
    "run_citation_gate",
    "run_citation_agent",
    "cite_draft",
    "lks_retriever_factory",
    "citation_false_pass_rate",
]
