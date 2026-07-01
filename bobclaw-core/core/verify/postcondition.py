"""BoBClaw Core — verification spine §2.6 **tier-1**: cross-family post-condition critic.

The actor emits an explicit **expected post-condition** for a step; a **decorrelated**
(cross-family) critic verifies *that the declared post-condition HOLDS* given the resulting
state — NOT that the raw output changed. A "did the output/screen change" audit is exactly
what this replaces (§2.6 + §4): a changed-but-unsatisfied output is a VIOLATION.

Decorrelation (§2.4 JOAT routing) is the heart of tier-1 and is deterministic + testable:
given the actor's backend, ``decorrelated_critic_backend`` returns a backend in a DIFFERENT
model family (``deepseek`` / ``glm`` / ``kimi`` / ``claude`` / ``gemini`` / ``minimax`` / … are
distinct). It reuses ``core.teams.role_backend`` for the team's critic candidate, then a fixed
cross-family preference — never re-implementing routing, never returning a same-family critic.

Fail-safe posture: only ``holds`` → ``passed=True``; ``violated`` / ``unknown`` / an unreachable
critic → ``passed=False`` (tier-1 never auto-passes on ambiguity; Default-FAIL termination is the
MS-3 tier-2 concern). Import-light: the real backend send is a LAZY import behind an injectable
``send`` callable, so importing this module pulls no heavy modules and unit tests stay pure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, Sequence

logger = logging.getLogger(__name__)


class PostConditionError(RuntimeError):
    """Tier-1 post-condition error: cannot resolve a decorrelated critic / a correlated override."""


class PCVerdict(str, Enum):
    HOLDS = "holds"        # the resulting state SATISFIES the declared post-condition -> pass
    VIOLATED = "violated"  # the resulting state does NOT satisfy it (incl. changed-but-unmet) -> catch
    UNKNOWN = "unknown"    # insufficient evidence / critic unreachable / unparseable -> NOT a pass


# ── Family taxonomy (the decorrelation map) ─────────────────────────────────────
# Every registered backend (+ the claude_managed alias) → its model family. An UNMAPPED
# backend falls back to its own bare string as family (a novel backend never silently
# shares a family). codex_code is treated as its own "codex" family by harness identity
# (its underlying litellm model is configurable); opencode_serve / local likewise.
FAMILY_BY_BACKEND: dict[str, str] = {
    "claude_api": "claude",
    "claude_code": "claude",
    "claude_managed": "claude",
    "agy_code": "gemini",
    "gemini_flash": "gemini",
    "gemini_pro": "gemini",
    "gemini_deep_research": "gemini",
    "kimi_platform": "kimi",
    "kimi_code": "kimi",
    "kimi_cli": "kimi",
    "deepseek_v4_flash": "deepseek",
    "glm_5_2": "glm",
    "minimax": "minimax",
    "codex_code": "codex",
    "opencode_serve": "opencode",
    "local": "local",
    # MS2-G4: the GUI grounding head (Holo-3.1, served on local llama.cpp) is its OWN family, so when
    # the GUI *actor* is the Holo head, the Tier-2 critic (G3) + the recovery adjudicator (G7) can never
    # silently pick a same-family critic (DESIGN-MS-D1 §5). Purely additive — every existing lookup is
    # unchanged, so the no-Holo path is byte-identical.
    "holo_grounder": "holo",
    "holo": "holo",
    # MS2-R0: the research floor (Qwen 35B-A3B, served on local llama.cpp) is its OWN family, so when
    # the research ASSERTER is the Qwen head, R4's entailment critic + R5's adversarial refuter can never
    # silently pick a same-family backend (DESIGN-MS-D2 §4 OD#6, the 3-family decorrelation: asserter ≠
    # critic ≠ refuter). Purely additive — every existing lookup is unchanged, so the no-Qwen path is
    # byte-identical.
    "qwen_research": "qwen",
}

# Ordered, real-backend preference spanning SIX families — so a cross-family critic is ALWAYS
# resolvable for any single-family actor (cheap-first).
DEFAULT_CRITIC_PREFERENCE: tuple[str, ...] = (
    "glm_5_2",
    "deepseek_v4_flash",
    "minimax",
    "kimi_code",
    "claude_api",
    "gemini_pro",
)


def family_of(backend: str) -> str:
    """The model family for a backend string (unmapped → its own bare string). Pure."""
    return FAMILY_BY_BACKEND.get(backend, backend or "unknown")


def is_decorrelated(actor_backend: str, critic_backend: str) -> bool:
    """True iff actor and critic belong to DIFFERENT model families. Pure."""
    return family_of(actor_backend) != family_of(critic_backend)


def decorrelated_critic_backend(
    actor_backend: str,
    *,
    team: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
) -> str:
    """Resolve a critic backend in a different family than *actor_backend*.

    Candidate pool order: the team's bound critic (``core.teams.role_backend(team, "critic")``,
    JOAT routing reuse) → *candidates* → ``DEFAULT_CRITIC_PREFERENCE``. Returns the FIRST whose
    family differs from the actor's. Raises ``PostConditionError`` if none qualifies. NEVER
    returns a same-family backend (the decorrelation guarantee).
    """
    actor_family = family_of(actor_backend)
    pool: list[str] = []

    if team is not None:
        # Lazy import keeps this module decoupled from the teams import graph at load.
        from core.teams import role_backend

        team_critic = role_backend(team, "critic")
        if team_critic:
            pool.append(team_critic)

    if candidates:
        pool.extend(candidates)

    pool.extend(DEFAULT_CRITIC_PREFERENCE)

    for candidate in pool:
        if candidate and family_of(candidate) != actor_family:
            return candidate

    raise PostConditionError(
        f"No decorrelated critic backend found for actor_backend={actor_backend!r} "
        f"(family {actor_family!r}); pool={pool!r}"
    )


# Brace-safe: rendered via str.replace (NOT str.format), so step/statement/result may contain
# arbitrary literal { } (JSON/code post-conditions are common) without a KeyError/ValueError, and
# the JSON example below stays single-braced.
PC_PROMPT_TEMPLATE: str = """You are an impartial verifier deciding whether a declared post-condition is satisfied.

Step performed:
{step}

Actor's declared expected post-condition (what the step MUST achieve):
{statement}

Resulting state / artifact AFTER the step executed:
{result}

Your task: judge ONLY whether the declared post-condition is satisfied by the resulting state.
- Do NOT pass merely because the output or screen CHANGED. A changed output that does not satisfy the post-condition is a VIOLATION.
- If the resulting state clearly satisfies the post-condition, answer "holds".
- If the resulting state clearly fails to satisfy it (including changed-but-unmet), answer "violated".
- If there is insufficient evidence to decide, answer "unknown".

Respond with a SINGLE line of JSON in exactly this format and nothing else:
{"verdict":"holds"|"violated"|"unknown","reasons":["short reason", "..."]}"""


def _render_prompt(template: str, step: str, statement: str, result: str) -> str:
    """Substitute the three fields BRACE-SAFELY (str.replace, not str.format) so arbitrary
    content with literal { } never breaks rendering."""
    return (
        template.replace("{step}", step)
        .replace("{statement}", statement)
        .replace("{result}", result)
    )


def build_pc_prompt(step: str, statement: str, result: str) -> str:
    """Render the post-condition critic prompt with the given step/statement/result (brace-safe)."""
    return _render_prompt(PC_PROMPT_TEMPLATE, step, statement, result)


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort JSON object extraction from a critic reply (self-contained; import-light).

    Strips ``` fences, then tries the whole string, a ``"verdict"``-bearing object, and finally
    the first balanced-looking ``{...}``.
    """
    text = re.sub(r"^```(?:json)?\s*\n?", "", text or "", flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


def parse_pc_verdict(raw: str) -> tuple[PCVerdict, list[str]]:
    """Parse a critic reply into ``(PCVerdict, reasons)``.

    An unparseable reply or an unknown/malformed verdict string → ``(PCVerdict.UNKNOWN,
    ["parse_error: ..."])``. ``reasons`` defaults to ``[]``. Pure.
    """
    data = _extract_json(raw or "")
    if not isinstance(data, dict):
        return (PCVerdict.UNKNOWN, ["parse_error: could not extract JSON"])

    verdict_str = str(data.get("verdict", "")).strip().lower()
    raw_reasons = data.get("reasons")
    if isinstance(raw_reasons, list):
        reasons = [str(r) for r in raw_reasons]
    elif raw_reasons:
        reasons = [str(raw_reasons)]
    else:
        reasons = []

    try:
        verdict = PCVerdict(verdict_str)
    except ValueError:
        return (PCVerdict.UNKNOWN, [f"parse_error: unknown verdict {verdict_str!r}"])
    return (verdict, reasons)


@dataclass(frozen=True)
class PostConditionResult:
    verdict: PCVerdict
    passed: bool            # verdict is PCVerdict.HOLDS
    reasons: tuple[str, ...]
    actor_backend: str
    critic_backend: str
    decorrelated: bool      # is_decorrelated(actor, critic) — always True on a successful verify

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "actor_backend": self.actor_backend,
            "critic_backend": self.critic_backend,
            "decorrelated": self.decorrelated,
        }


async def _default_send(messages: list[dict], backend: str) -> str:
    """Lazy real-backend send (kept out of module import so this file stays import-light)."""
    from core.nodes.execute import _send_to_backend

    return await _send_to_backend(messages, backend)


async def verify_post_condition(
    *,
    step: str,
    statement: str,
    result: str,
    actor_backend: str,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable[[list[dict], str], Awaitable[str]]] = None,
    prompt_template: Optional[str] = None,
) -> PostConditionResult:
    """Verify a declared post-condition with a decorrelated cross-family critic.

    Resolves (and ENFORCES) a cross-family critic: a caller-supplied SAME-family
    ``critic_backend`` is rejected with ``PostConditionError`` — decorrelation is
    non-negotiable. A critic that returns ``violated``/``unknown`` or is unreachable →
    ``passed=False`` (fail-safe; tier-1 never auto-passes on ambiguity).
    """
    # Fail-safe at the core: an empty/whitespace post-condition is undecidable — refuse it
    # WITHOUT calling a critic (a lenient critic could otherwise 'holds' a blank prompt). This
    # protects every direct caller (the node, the verifier, and MS-3's tier-2 reuse).
    if not (statement or "").strip():
        return PostConditionResult(
            verdict=PCVerdict.UNKNOWN,
            passed=False,
            reasons=("no post-condition declared",),
            actor_backend=actor_backend,
            critic_backend=critic_backend or "",
            decorrelated=False,
        )

    crit = critic_backend or decorrelated_critic_backend(actor_backend, team=team)
    if not is_decorrelated(actor_backend, crit):
        raise PostConditionError(
            f"critic backend {crit!r} (family {family_of(crit)!r}) is NOT decorrelated from "
            f"actor {actor_backend!r} (family {family_of(actor_backend)!r})"
        )

    template = prompt_template or PC_PROMPT_TEMPLATE
    prompt = _render_prompt(template, step, statement, result)
    messages = [
        {"role": "system", "content": "You are an impartial post-condition verifier."},
        {"role": "user", "content": prompt},
    ]

    send_fn = send or _default_send
    try:
        raw = await send_fn(messages, crit)
    except asyncio.CancelledError:  # noqa: BLE001 — cancellation must not auto-pass
        return PostConditionResult(
            verdict=PCVerdict.UNKNOWN,
            passed=False,
            reasons=("critic verification cancelled",),
            actor_backend=actor_backend,
            critic_backend=crit,
            decorrelated=True,
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe: a critic failure NEVER auto-passes
        logger.warning("post-condition critic call failed (backend=%r): %s", crit, exc)
        return PostConditionResult(
            verdict=PCVerdict.UNKNOWN,
            passed=False,
            reasons=(f"critic_unavailable: {type(exc).__name__}: {exc}",),
            actor_backend=actor_backend,
            critic_backend=crit,
            decorrelated=True,
        )

    verdict, reasons = parse_pc_verdict(raw)
    return PostConditionResult(
        verdict=verdict,
        passed=verdict is PCVerdict.HOLDS,
        reasons=tuple(reasons),
        actor_backend=actor_backend,
        critic_backend=crit,
        decorrelated=True,
    )


def _run_blocking(make_coro: Callable[[], Awaitable]):
    """Run an async coroutine to completion from a SYNC context, loop-safe.

    No running loop → ``asyncio.run``. If a loop IS already running in this thread (e.g. the
    sync verifier is called from inside async code), run it in a fresh worker thread with its own
    loop instead of raising ``RuntimeError`` — so the verifier is safe in any context.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(make_coro())).result()


def make_postcondition_verifier(
    *,
    team: Optional[str] = None,
    critic_backend: Optional[str] = None,
    send: Optional[Callable[[list[dict], str], Awaitable[str]]] = None,
    default_actor_backend: Optional[str] = None,
) -> Callable[[dict], bool]:
    """A SYNC ``verifier(payload) -> bool`` adapter for ``core.ses.falsepass.false_pass_rate``.

    *payload* is a dict ``{step, statement (or "post_condition"), result, actor_backend?}``; the
    ground-truth label is NEVER in the payload (``false_pass_rate`` passes only ``item.payload``).
    Drives :func:`verify_post_condition` via :func:`_run_blocking` (``asyncio.run`` with a
    worker-thread fallback if a loop is already running) and returns ``res.passed``.
    """

    def verifier(payload: dict) -> bool:
        if not isinstance(payload, dict):
            raise PostConditionError(
                f"post-condition verifier payload must be a dict, got {type(payload).__name__}"
            )
        statement = str(payload.get("statement") or payload.get("post_condition") or "")
        if not statement.strip():
            # Matches postcondition_node's fail-safe: no declared condition → not passed.
            return False
        actor = payload.get("actor_backend") or default_actor_backend or "local"
        try:
            res = _run_blocking(
                lambda: verify_post_condition(
                    step=str(payload.get("step", "")),
                    statement=statement,
                    result=str(payload.get("result", "")),
                    actor_backend=actor,
                    team=team,
                    critic_backend=critic_backend,
                    send=send,
                )
            )
        except asyncio.CancelledError as exc:  # noqa: BLE001 — measurement boundary: never auto-pass
            logger.warning("post-condition verifier cancelled (actor=%r): %s", actor, exc)
            return False
        except Exception as exc:  # noqa: BLE001 — measurement boundary: never auto-pass, never abort
            # The verifier is the false_pass_rate boundary (that loop has no try/except), so ANY
            # failure → not passed (never an auto-pass), and the measurement keeps running. A
            # config error (same-family override / no decorrelated critic) or a systemic bug
            # surfaces as a false_fail_rate spike, never a deceptive 0.0 false-pass. (Transient
            # critic failures are already handled inside verify as UNKNOWN.)
            logger.warning("post-condition verifier failed (actor=%r): %s", actor, exc)
            return False
        return res.passed

    return verifier
