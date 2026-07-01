from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
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

from core.ledger.bidkey import bid_key as _bid_key
from core.ledger.erg import on_entailment_failure as _on_entailment_failure
from core.ledger.erg import validate_reason as _validate_reason
from core.ledger.types import (
    ClaimStatus,
    EXHAUSTED_TAG,
    RETRY_LIMIT,
    RetryReason,
)
from core.verify.postcondition import (
    _run_blocking,
    decorrelated_critic_backend as _decorrelated_critic_backend,
    family_of as _family_of,
    is_decorrelated as _is_decorrelated,
    PostConditionError,
)

log = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────

class EntailmentError(RuntimeError):
    """Tier-2 entailment error: cannot resolve a decorrelated critic / a same-family override."""


# ── Enums ────────────────────────────────────────────────────────────────────

class EntailmentVerdict(str, Enum):
    ENTAILED = "entailed"
    NOT_ENTAILED = "not_entailed"
    UNKNOWN = "unknown"


class VerificationTag(str, Enum):
    PV = "PV"  # primary-verified
    VS = "VS"  # vendor-stated
    U = "U"    # unverified


class SourceKind(str, Enum):
    PRIMARY = "primary"
    VENDOR = "vendor"


# ── Frozen dataclasses ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    id: str
    text: str
    kind: SourceKind = SourceKind.PRIMARY

    @classmethod
    def coerce(cls, obj: Any) -> Source:
        if isinstance(obj, Source):
            return obj
        if isinstance(obj, dict):
            kind = obj.get("kind", SourceKind.PRIMARY)
            if isinstance(kind, str):
                kind = SourceKind(kind)
            return cls(
                id=str(obj["id"]),
                text=str(obj["text"]),
                kind=kind,
            )
        raise TypeError(f"Cannot coerce {type(obj)} to Source")


@dataclass(frozen=True)
class Claim:
    subject: str
    predicate: str
    numeric_value: object = None
    cited_source_id: Optional[str] = None
    text: str = ""

    @property
    def bid_key(self) -> str:
        return _bid_key(self.subject, self.predicate, self.numeric_value)

    def render(self) -> str:
        if self.text:
            return self.text
        return f"{self.subject} {self.predicate} {self.numeric_value}"

    @classmethod
    def coerce(cls, obj: Any) -> Claim:
        if isinstance(obj, Claim):
            return obj
        if isinstance(obj, dict):
            return cls(
                subject=str(obj["subject"]),
                predicate=str(obj["predicate"]),
                numeric_value=obj.get("numeric_value"),
                cited_source_id=obj.get("cited_source_id"),
                text=obj.get("text", ""),
            )
        raise TypeError(f"Cannot coerce {type(obj)} to Claim")


# ── Prompt building ─────────────────────────────────────────────────────────

ENTAILMENT_PROMPT_TEMPLATE: str = (
    'You are an impartial entailment judge. '
    'Given a claim and a source text, determine if the source text explicitly supports '
    'the SPECIFIC NUMBER claimed. '
    'Do NOT answer "entailed" merely because a citation exists or the topic matches. '
    'If the source does not state (or contradicts) the number, answer "not_entailed". '
    'If the source is silent or insufficient, answer "unknown". '
    'If "not_entailed", optionally classify with ONE bounded code from: '
    'TEMPORAL_SCOPE_MISMATCH, WRONG_ENTITY, STALE_SOURCE, NUMERIC_MISMATCH, UNSUPPORTED (or null). '
    'Respond with a SINGLE line of JSON:\n'
    '{"verdict":"entailed"|"not_entailed"|"unknown","reasons":["..."],"reason_code":"<CODE>"|null}\n'
    'Claim: {claim_text}\n'
    'Number: {numeric_value}\n'
    'Source text: {source_text}'
)


def _safe_format(template: str, **kwargs: str) -> str:
    """Single-pass template substitution.

    Unlike sequential ``str.replace``, braces inside substituted values are
    never rescanned, so user-supplied text cannot inject later placeholders.
    """

    def _repl(match: re.Match) -> str:
        key = match.group(1)
        return kwargs.get(key, match.group(0))

    return re.sub(r"\{(\w+)\}", _repl, template)


def build_entailment_prompt(claim_text: str, numeric_value: object, source_text: str) -> str:
    """Render the prompt with BRACE-SAFE substitution."""
    return _safe_format(
        ENTAILMENT_PROMPT_TEMPLATE,
        claim_text=claim_text,
        numeric_value=str(numeric_value),
        source_text=source_text,
    )


# ── Verdict parsing ─────────────────────────────────────────────────────────

def parse_entailment_verdict(raw: str) -> Tuple[EntailmentVerdict, List[str], Optional[RetryReason]]:
    """Extract JSON from raw response; return (verdict, reasons, reason_code)."""
    # Remove possible markdown fences
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove leading fence
        lines = cleaned.splitlines()
        # Find first '{' after fence
        json_start = None
        for i, line in enumerate(lines):
            if "{" in line:
                json_start = i
                break
        if json_start is None:
            return _unknown_parse(f"No JSON found in fenced block: {raw[:200]}")
        # Rejoin from json_start to end before closing fence
        json_lines = lines[json_start:]
        # Remove trailing fence if any
        for j in range(len(json_lines) - 1, -1, -1):
            if "```" in json_lines[j]:
                json_lines = json_lines[:j]
                break
        cleaned = "\n".join(json_lines).strip()
    # Collect ALL balanced top-level { ... } substrings, QUOTE/ESCAPE-AWARE (audit r3-S2): braces inside
    # a JSON string literal (e.g. a reason of "see } here") must NOT change the depth. Then pick the LAST
    # object carrying a "verdict" key (audit r6-S2): the model's ACTUAL answer is the FINAL JSON object —
    # a spoof object embedded earlier in the prose reasoning (e.g. '... {"verdict":"entailed"} ... <real>')
    # must NEVER win and false-pass the gate. The "single line of JSON" answer is the trailing one.
    candidates: List[str] = []
    brace_depth = 0
    start_idx = None
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
            if brace_depth == 0:
                start_idx = i
            brace_depth += 1
        elif ch == "}":
            if brace_depth > 0:
                brace_depth -= 1
                if brace_depth == 0 and start_idx is not None:
                    candidates.append(cleaned[start_idx : i + 1])
                    start_idx = None
    if not candidates:
        return _unknown_parse(f"No balanced JSON structure: {raw[:200]}")

    # The LAST verdict-bearing dict wins; else the last parseable dict; else fail-safe UNKNOWN.
    data = None
    fallback = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            fallback = obj
            if "verdict" in obj:
                data = obj
    if data is None:
        data = fallback
    if not isinstance(data, dict):
        return _unknown_parse(f"No verdict-bearing JSON object: {raw[:200]}")

    verdict_str = data.get("verdict", "unknown")
    if isinstance(verdict_str, str):
        try:
            verdict = EntailmentVerdict(verdict_str.lower())
        except ValueError:
            verdict = EntailmentVerdict.UNKNOWN
    else:
        verdict = EntailmentVerdict.UNKNOWN

    reasons_raw = data.get("reasons", [])
    if not isinstance(reasons_raw, list):
        reasons_raw = [str(reasons_raw)]
    reasons = [str(r) for r in reasons_raw]

    reason_code_raw = data.get("reason_code", None)
    reason_code: Optional[RetryReason] = None
    if reason_code_raw is not None:
        code_str = str(reason_code_raw).strip()
        if code_str and _validate_reason(code_str):
            reason_code = RetryReason(code_str)

    return verdict, reasons, reason_code


def _unknown_parse(msg: str) -> Tuple[EntailmentVerdict, List[str], Optional[RetryReason]]:
    return (EntailmentVerdict.UNKNOWN, [f"parse_error: {msg}"], None)


# ── Tag mapping ─────────────────────────────────────────────────────────────

def tag_for(verdict: EntailmentVerdict, source_kind: SourceKind) -> VerificationTag:
    if verdict == EntailmentVerdict.ENTAILED:
        if source_kind == SourceKind.PRIMARY:
            return VerificationTag.PV
        else:
            return VerificationTag.VS
    return VerificationTag.U


# ── EntailmentResult ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntailmentResult:
    verdict: EntailmentVerdict
    entailed: bool
    tag: VerificationTag
    reasons: Tuple[str, ...]
    reason_code: Optional[RetryReason]
    bid_key: str
    cited_source_id: Optional[str]
    actor_backend: str
    critic_backend: str
    decorrelated: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "entailed": self.entailed,
            "tag": self.tag.value,
            "reasons": list(self.reasons),
            "reason_code": self.reason_code.value if self.reason_code else None,
            "bid_key": self.bid_key,
            "cited_source_id": self.cited_source_id,
            "actor_backend": self.actor_backend,
            "critic_backend": self.critic_backend,
            "decorrelated": self.decorrelated,
        }


# ── Lazy send (default backend) ─────────────────────────────────────────────

def _default_send(messages: List[Dict[str, Any]], backend: str) -> Awaitable[str]:
    # Lazy import to keep import-light
    from core.nodes.execute import _send_to_backend as _real_send
    return _real_send(messages, backend)


# ── Core verify functions ────────────────────────────────────────────────────

async def verify_claim_against_source(
    *,
    claim: Claim,
    source: Source,
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable[[List[Dict[str, Any]], str], Awaitable[str]]] = None,
    prompt_template: Optional[str] = None,
) -> EntailmentResult:
    bk = claim.bid_key
    # Fail-safe: empty source text -> UNKNOWN without critic call
    if not source.text.strip():
        return EntailmentResult(
            verdict=EntailmentVerdict.UNKNOWN,
            entailed=False,
            tag=VerificationTag.U,
            reasons=("empty source: cannot entail",),
            reason_code=None,
            bid_key=bk,
            cited_source_id=claim.cited_source_id,
            actor_backend=actor_backend,
            critic_backend="",
            decorrelated=False,
        )
    # Resolve + ENFORCE a decorrelated (cross-family) critic.
    if critic_backend is None:
        try:
            critic_backend = _decorrelated_critic_backend(actor_backend, team=team)
        except PostConditionError as e:
            raise EntailmentError(str(e)) from e
    else:
        if not _is_decorrelated(actor_backend, critic_backend):
            raise EntailmentError(
                f"Explicit critic_backend '{critic_backend}' is same family as actor '{actor_backend}'"
            )
    decorrelated = _is_decorrelated(actor_backend, critic_backend)
    if not decorrelated:
        raise EntailmentError(
            f"Critic backend '{critic_backend}' is not decorrelated from actor '{actor_backend}'"
        )
    # Build prompt
    if prompt_template:
        prompt = _safe_format(
            prompt_template,
            claim_text=claim.render(),
            numeric_value=str(claim.numeric_value),
            source_text=source.text,
        )
    else:
        prompt = build_entailment_prompt(claim.render(), claim.numeric_value, source.text)
    messages = [{"role": "user", "content": prompt}]
    # Send
    effective_send = send or _default_send
    try:
        raw_response = await effective_send(messages, critic_backend)
    except asyncio.CancelledError:
        return EntailmentResult(
            verdict=EntailmentVerdict.UNKNOWN,
            entailed=False,
            tag=VerificationTag.U,
            reasons=("critic_unavailable: CancelledError",),
            reason_code=None,
            bid_key=bk,
            cited_source_id=claim.cited_source_id,
            actor_backend=actor_backend,
            critic_backend=critic_backend,
            decorrelated=decorrelated,
        )
    except Exception as e:
        return EntailmentResult(
            verdict=EntailmentVerdict.UNKNOWN,
            entailed=False,
            tag=VerificationTag.U,
            reasons=(f"critic_unavailable: {type(e).__name__}: {e}",),
            reason_code=None,
            bid_key=bk,
            cited_source_id=claim.cited_source_id,
            actor_backend=actor_backend,
            critic_backend=critic_backend,
            decorrelated=decorrelated,
        )
    # Parse
    verdict, reasons, reason_code = parse_entailment_verdict(raw_response)
    entailed = verdict == EntailmentVerdict.ENTAILED
    tag = tag_for(verdict, source.kind)
    return EntailmentResult(
        verdict=verdict,
        entailed=entailed,
        tag=tag,
        reasons=tuple(reasons),
        reason_code=reason_code,
        bid_key=bk,
        cited_source_id=claim.cited_source_id,
        actor_backend=actor_backend,
        critic_backend=critic_backend,
        decorrelated=decorrelated,
    )


async def verify_claim(
    *,
    claim: Claim,
    sources: Mapping[str, Source] | Sequence[Source],
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable[[List[Dict[str, Any]], str], Awaitable[str]]] = None,
    prompt_template: Optional[str] = None,
) -> EntailmentResult:
    bk = claim.bid_key
    # Resolve cited_source_id
    if not claim.cited_source_id:
        return EntailmentResult(
            verdict=EntailmentVerdict.UNKNOWN,
            entailed=False,
            tag=VerificationTag.U,
            reasons=("no cited source",),
            reason_code=None,
            bid_key=bk,
            cited_source_id=None,
            actor_backend=actor_backend,
            critic_backend="",
            decorrelated=False,
        )
    # Normalize sources to mapping
    if isinstance(sources, Sequence):
        sources_map = {src.id: src for src in sources}
    else:
        sources_map = dict(sources)
    source = sources_map.get(claim.cited_source_id)
    if source is None:
        return EntailmentResult(
            verdict=EntailmentVerdict.UNKNOWN,
            entailed=False,
            tag=VerificationTag.U,
            reasons=(f"cited source {claim.cited_source_id} not found",),
            reason_code=None,
            bid_key=bk,
            cited_source_id=claim.cited_source_id,
            actor_backend=actor_backend,
            critic_backend="",
            decorrelated=False,
        )
    return await verify_claim_against_source(
        claim=claim,
        source=source,
        actor_backend=actor_backend,
        team=team,
        critic_backend=critic_backend,
        send=send,
        prompt_template=prompt_template,
    )


# ── ERG gate helpers ────────────────────────────────────────────────────────

def new_gate_entry(bid_key: str) -> Dict[str, Any]:
    return {
        "bid_key": bid_key,
        "retry_count": 0,
        "tried_sources": [],
        "status": ClaimStatus.PENDING.value,
    }


@dataclass(frozen=True)
class RetrieveRequest:
    bid_key: str
    tried_sources: Tuple[str, ...]
    constraint: Optional[str]
    reason_code: Optional[str]
    attempt: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bid_key": self.bid_key,
            "tried_sources": list(self.tried_sources),
            "constraint": self.constraint,
            "reason_code": self.reason_code,
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class GateOutcome:
    bid_key: str
    final_tag: VerificationTag
    status: str
    verified: bool
    exhausted: bool
    committed_tag: Optional[str]
    attempts: Tuple[EntailmentResult, ...]
    directives: Tuple[Dict[str, Any], ...]
    entry: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bid_key": self.bid_key,
            "final_tag": self.final_tag.value,
            "status": self.status,
            "verified": self.verified,
            "exhausted": self.exhausted,
            "committed_tag": self.committed_tag,
            "attempts": [r.as_dict() for r in self.attempts],
            "directives": list(self.directives),
            "entry": self.entry,
        }


async def run_entailment_gate(
    *,
    claim: Claim,
    retrieve: Callable[[RetrieveRequest], Awaitable[Optional[Union[Source, Dict[str, Any]]]]],
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable[[List[Dict[str, Any]], str], Awaitable[str]]] = None,
    gate_entry: Optional[Dict[str, Any]] = None,
    max_attempts: int = 8,
) -> GateOutcome:
    bk = claim.bid_key
    entry = gate_entry if gate_entry is not None else new_gate_entry(bk)
    attempts: List[EntailmentResult] = []
    directives: List[Dict[str, Any]] = []
    constraint: Optional[str] = None
    reason_code: Optional[str] = None
    attempt = 0
    while attempt < max_attempts:
        req = RetrieveRequest(
            bid_key=bk,
            tried_sources=tuple(entry["tried_sources"]),
            constraint=constraint,
            reason_code=reason_code,
            attempt=attempt,
        )
        src = await retrieve(req)
        if src is None:
            # No decorrelated source available -> treat as a failed attempt. A CONSTANT sentinel
            # (not attempt-indexed) so on_entailment_failure dedups a repeated no-source into one
            # tried_sources entry (retry_count still advances -> the ERG limit terminates).
            src_id = "<no-source>"
            res = EntailmentResult(
                verdict=EntailmentVerdict.UNKNOWN,
                entailed=False,
                tag=VerificationTag.U,
                reasons=(f"no source returned at attempt {attempt}",),
                reason_code=None,
                bid_key=bk,
                cited_source_id=claim.cited_source_id,
                actor_backend=actor_backend,
                critic_backend="",
                decorrelated=False,
            )
        else:
            try:
                source = Source.coerce(src)
            except (KeyError, TypeError, ValueError) as e:
                # audit r2-S4: a malformed retrieve result (missing id/text, bad kind) is a BAD SOURCE,
                # not a crash — record a not-entailed U attempt and route it through the ERG failure
                # path below (so a different decorrelated source can be re-branched to). Never aborts.
                src_id = "<malformed-source>"
                res = EntailmentResult(
                    verdict=EntailmentVerdict.UNKNOWN,
                    entailed=False,
                    tag=VerificationTag.U,
                    reasons=(f"malformed source from retrieve: {type(e).__name__}: {e}",),
                    reason_code=None,
                    bid_key=bk,
                    cited_source_id=claim.cited_source_id,
                    actor_backend=actor_backend,
                    critic_backend="",
                    decorrelated=False,
                )
                attempts.append(res)
                out = _on_entailment_failure(entry, src_id, reason=None)
                entry = out["entry"]
                directive = out["directive"]
                directives.append(directive)
                if directive["action"] == "RE_BRANCH":
                    constraint = directive.get("constraint")
                    reason_code = directive.get("reason")
                    attempt += 1
                    continue
                entry["status"] = ClaimStatus.UNVERIFIED_EXHAUSTED.value
                return GateOutcome(
                    bid_key=bk,
                    final_tag=VerificationTag.U,
                    status=ClaimStatus.UNVERIFIED_EXHAUSTED.value,
                    verified=False,
                    exhausted=True,
                    committed_tag=directive.get("status_tag", EXHAUSTED_TAG),
                    attempts=tuple(attempts),
                    directives=tuple(directives),
                    entry=entry,
                )
            try:
                res = await verify_claim_against_source(
                    claim=claim,
                    source=source,
                    actor_backend=actor_backend,
                    team=team,
                    critic_backend=critic_backend,
                    send=send,
                )
            except EntailmentError as e:
                # The gate degrades gracefully: a critic-resolution/config error is NOT a "wrong
                # source we can re-search past" — it's an unresolved could-not-verify (tag U). Finalize
                # immediately as PENDING/U (NOT exhausted) so Default-FAIL termination REVERTs the merge
                # (an empty/unresolved criterion blocks fast-forward), and the claim is surfaced.
                res = EntailmentResult(
                    verdict=EntailmentVerdict.UNKNOWN,
                    entailed=False,
                    tag=VerificationTag.U,
                    reasons=(f"entailment_error: {e}",),
                    reason_code=None,
                    bid_key=bk,
                    cited_source_id=claim.cited_source_id,
                    actor_backend=actor_backend,
                    critic_backend=critic_backend or "",
                    decorrelated=False,
                )
                attempts.append(res)
                return GateOutcome(
                    bid_key=bk,
                    final_tag=VerificationTag.U,
                    status=entry.get("status", ClaimStatus.PENDING.value),
                    verified=False,
                    exhausted=False,
                    committed_tag=None,
                    attempts=tuple(attempts),
                    directives=tuple(directives),
                    entry=entry,
                )
            src_id = source.id
        attempts.append(res)

        if res.entailed:
            entry["status"] = ClaimStatus.VERIFIED.value
            return GateOutcome(
                bid_key=bk,
                final_tag=res.tag,
                status=ClaimStatus.VERIFIED.value,
                verified=True,
                exhausted=False,
                committed_tag=None,
                attempts=tuple(attempts),
                directives=tuple(directives),
                entry=entry,
            )

        # ERG failure
        failure_reason: Optional[str] = res.reason_code.value if res.reason_code else None
        out = _on_entailment_failure(entry, src_id, reason=failure_reason)
        entry = out["entry"]
        directive = out["directive"]
        directives.append(directive)

        if directive["action"] == "RE_BRANCH":
            constraint = directive.get("constraint")
            reason_code = directive.get("reason")
            # FIREWALL: only constraint and reason_code cross – res.reasons NEVER in directive
            attempt += 1
            continue
        else:
            # EXHAUSTED_SEARCH
            entry["status"] = ClaimStatus.UNVERIFIED_EXHAUSTED.value
            committed_tag = directive.get("status_tag", EXHAUSTED_TAG)
            return GateOutcome(
                bid_key=bk,
                final_tag=VerificationTag.U,
                status=ClaimStatus.UNVERIFIED_EXHAUSTED.value,
                verified=False,
                exhausted=True,
                committed_tag=committed_tag,
                attempts=tuple(attempts),
                directives=tuple(directives),
                entry=entry,
            )

    # Belt: max_attempts hit without resolution
    entry["status"] = ClaimStatus.UNVERIFIED_EXHAUSTED.value
    return GateOutcome(
        bid_key=bk,
        final_tag=VerificationTag.U,
        status=ClaimStatus.UNVERIFIED_EXHAUSTED.value,
        verified=False,
        exhausted=True,
        committed_tag=EXHAUSTED_TAG,
        attempts=tuple(attempts),
        directives=tuple(directives),
        entry=entry,
    )


# ── Sync verifier for false-pass measurement ────────────────────────────────

def make_entailment_verifier(
    *,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable[[List[Dict[str, Any]], str], Awaitable[str]]] = None,
    default_actor_backend: Optional[str] = None,
) -> Callable[[Dict[str, Any]], bool]:
    async def _async_verify(payload: Dict[str, Any]) -> bool:
        actor = payload.get("actor_backend", default_actor_backend)
        if not actor:
            raise ValueError("No actor_backend in payload and no default")
        claim = Claim.coerce(payload)
        source_text = payload.get("source_text", "")
        if not source_text.strip():
            return False
        source_kind_str = payload.get("source_kind", "primary")
        try:
            source_kind = SourceKind(source_kind_str)
        except ValueError:
            source_kind = SourceKind.PRIMARY
        source_id = payload.get("source_id", "verifier-source")
        source = Source(id=source_id, text=source_text, kind=source_kind)
        res = await verify_claim_against_source(
            claim=claim,
            source=source,
            actor_backend=actor,
            team=team,
            critic_backend=critic_backend,
            send=send,
        )
        return res.entailed

    def verifier(payload: Dict[str, Any]) -> bool:
        # _run_blocking expects a ZERO-ARG callable returning a coroutine (it calls make_coro()),
        # so wrap in a lambda — passing the coroutine directly would break the asyncio.run(make_coro())
        # contract. The measurement boundary NEVER auto-passes: any failure -> False (false_pass_rate
        # has no try/except, so a config/systemic error surfaces as a false_fail spike, not a deceptive
        # 0.0 false-pass). CancelledError (BaseException) is caught here too so the measurement loop
        # is not aborted mid-set.
        try:
            return bool(_run_blocking(lambda: _async_verify(payload)))
        except asyncio.CancelledError as e:  # noqa: BLE001 — measurement boundary, never auto-pass
            log.warning("Entailment verifier cancelled: %s", e)
            return False
        except Exception as e:  # noqa: BLE001 — measurement boundary, never auto-pass / never abort
            log.warning("Entailment verifier exception: %s", e)
            return False

    return verifier


# ── Surface could-not-verify ────────────────────────────────────────────────

def surface_could_not_verify(
    results: Iterable[Union[EntailmentResult, GateOutcome, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    surfaced: List[Dict[str, Any]] = []
    for item in results:
        if isinstance(item, EntailmentResult):
            d = item.as_dict()
        elif isinstance(item, GateOutcome):
            d = item.as_dict()
        else:
            d = dict(item)
        # EntailmentResult.as_dict() carries "tag"; GateOutcome.as_dict() carries "final_tag" — read
        # both so a VERIFIED (PV/VS) GateOutcome is NOT wrongly surfaced via a defaulted "U".
        tag = d.get("tag", d.get("final_tag", "U"))
        if tag in ("U", VerificationTag.U.value):
            # §2.6/§2.9: the could-not-verify set must carry the REASON ("the reason stays in history").
            # An EntailmentResult has top-level "reasons"; a GateOutcome's reasons live in its attempts —
            # flatten them so the surfaced item is not reason-less (audit r5-S5).
            reasons = d.get("reasons")
            if reasons is None and isinstance(d.get("attempts"), list):
                reasons = [r for a in d["attempts"] for r in (a.get("reasons") or [])]
            surfaced.append(
                {
                    "bid_key": d.get("bid_key"),
                    "tag": tag,
                    "status": d.get("status"),
                    "reasons": reasons,
                    "cited_source_id": d.get("cited_source_id"),
                }
            )
    return surfaced
