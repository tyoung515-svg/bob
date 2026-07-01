"""
core/gui/dsl.py — MS2-G8 DSL intent-formalization ceiling for OPAQUE side-effects.

DESIGN-MS-D1 §3-G8 (Decision-3 SECOND, after the G2 anti-desync gate): the DSL ceiling for
OPAQUE side-effects — an action whose args carry NO risk signal but which acts destructively
*internally* (a stored proc that ``DROP``s, a script that wipes state; **especially the G6
escape-hatch scripts**). For such a plan the agent compiles its intent into a **DSL constraint**
that must "**compile**" against the §2.7 tiers (G1) before execution: every declared effect is
classified through the LANDED ``core.gui.tiers`` resolver, and a plan whose declared effects
reach Full-Access (or exceed the plan's declared ceiling) is REJECTED — routing it to the §2.7
human interrupt.

This is the ONLY place the DSL earns its complexity over the deterministic table, and only for
the opaque case: it is **NOT invoked** on actions the deterministic table already classifies
(``requires_dsl`` / ``make_dsl_compiler`` defer to the deterministic G1/G6 interrupt for a plan
already tiered Full-Access — *no over-reach*; the deterministic path is byte-identical).

Composition, not duplication:
  * the tier table = ``core.gui.tiers`` (``resolve_tier``/``requires_human``) — the DSL carries
    NO private tier map; "compile" is just running G1 over the declared effects;
  * the escape-hatch seam = ``core.gui.escape`` (``ScriptPlan``/``classify_script`` and the
    ``execute_via_escape(..., dsl_compile=...)`` hook) — ``make_dsl_compiler`` produces the
    ``Callable[[ScriptPlan], bool]`` the landed seam already accepts; ``escape.py`` is unchanged.

Deferred non-goal (DESIGN-MS-D1 §4/§5 [F3] Decision-3): **DSL-injection hardening** — verifying
that the declared constraint faithfully matches the opaque body (static/dynamic analysis of the
script ``code`` / proc body). G8 ships the *ceiling*, assuming an honest declaration; not the
hardening.

Purity guarantee: importing this module pulls in no backend / node / HTTP / sandbox module — only
the pure reused primitives (the G1 tier table + the G6 escape dataclasses) — makes no model call
and no I/O at import. The compile path runs zero model / zero Docker / zero network — pure
deterministic G1 arithmetic.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from core.gui.escape import ScriptPlan, classify_script
from core.gui.tiers import Tier, requires_human, resolve_tier

if TYPE_CHECKING:  # ``Scope`` is referenced only in string-quoted annotations — no runtime import
    import core.permissions as permissions


# ─── Exceptions ────────────────────────────────────────────────────────────────

class DslParseError(ValueError):
    """Raised when the DSL source text cannot be parsed according to the grammar."""


# ─── Status enum ─────────────────────────────────────────────────────────────────

class CompileStatus(str, Enum):
    """Result status of the compile-against-tiers check."""

    COMPILED = "compiled"              # declared effects all ≤ ceiling and none Full-Access → proceed
    REJECTED = "rejected"             # a declared effect is Full-Access OR exceeds the declared ceiling
    NO_DECLARATION = "no_declaration"  # opaque plan declared zero effects → fail closed
    PARSE_ERROR = "parse_error"       # the DSL source did not parse → fail closed


# ─── Frozen dataclasses (public) ───────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EffectDecl:
    """One declared opaque effect — the G1 classification unit.

    Attributes:
        op:   Operation / tool name, e.g. ``"drop"``, ``"write_file"``, ``"pay"``.
        args: Declared key-value arguments (by value). ``None`` when no args are given.
    """

    op: str
    args: "Mapping[str, object] | None" = None


@dataclass(frozen=True, slots=True)
class DslConstraint:
    """A parsed DSL constraint — the formal declaration of an opaque plan's effects.

    Attributes:
        ceiling: Maximum tier the plan claims to stay within (declared via ``ALLOW``).
        effects: Every declared effect, in order.
        raw:     Original source text (provenance).
    """

    ceiling: Tier
    effects: tuple[EffectDecl, ...]
    raw: str = ""


@dataclass(frozen=True, slots=True)
class CompileResult:
    """Outcome of compiling a DSL constraint against the G1 tier table.

    Attributes:
        status:         One of ``COMPILED`` / ``REJECTED`` / ``NO_DECLARATION`` / ``PARSE_ERROR``.
        compiles:       ``True`` iff *status* is ``COMPILED``.
        compiled_tier:  Maximum tier over all declared effect tiers (``Tier.READ_ONLY`` if none).
        ceiling:        The declared ceiling from the constraint.
        per_effect:     ``(op, tier)`` pairs in declared order.
        requires_human: ``True`` iff *compiled_tier* is ``Tier.FULL_ACCESS``.
        reason:         Human-readable explanation.
    """

    status: CompileStatus
    compiles: bool
    compiled_tier: Tier
    ceiling: Tier
    per_effect: tuple[tuple[str, Tier], ...]
    requires_human: bool
    reason: str


# ─── Internal grammar helpers ────────────────────────────────────────────────────

_TIER_LABEL_MAP: dict[str, Tier] = {
    # Canonical hyphenated labels (lower-cased lookup).
    "read-only": Tier.READ_ONLY,
    "write-local": Tier.WRITE_LOCAL,
    "social": Tier.SOCIAL,
    "full-access": Tier.FULL_ACCESS,
    # Enum names (lower-cased lookup).
    "read_only": Tier.READ_ONLY,
    "write_local": Tier.WRITE_LOCAL,
    "full_access": Tier.FULL_ACCESS,
}

_RE_ALLOW = re.compile(r"^\s*ALLOW\s+(.+?)\s*$", re.IGNORECASE)
_RE_EFFECT = re.compile(r"^\s*EFFECT\s+(.+?)\s*$", re.IGNORECASE)
_RE_COMMENT_OR_BLANK = re.compile(r"^\s*(#|$)")
# Token = a quote-aware ``key="value with spaces"`` pair, OR a bare quoted string, OR a non-space
# run. The first alternative keeps a quoted value (which may contain spaces) attached to its key.
_RE_TOKEN = re.compile(r'\S+?="[^"]*"|"[^"]*"|\S+')


def _normalize_tier(text: str) -> Tier:
    """Convert a user-supplied tier string to a ``Tier`` (canonical label OR enum name,
    case-insensitive). Raises ``DslParseError`` for an unrecognised tier."""
    key = text.strip().lower()
    if key in _TIER_LABEL_MAP:
        return _TIER_LABEL_MAP[key]
    raise DslParseError(f"unknown tier label: {text!r}")


def _strip_quotes(val: str) -> str:
    """Strip a single pair of surrounding double quotes, if present."""
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val


def _tokenize_effect_line(rest: str) -> tuple[str, "Mapping[str, object] | None"]:
    """Parse the remainder of an ``EFFECT`` line (after the leading ``EFFECT``).

    Returns ``(op, args_or_None)``. The first token is the operation name (required). Subsequent
    ``k=v`` tokens become args (value kept as a string; the FIRST ``=`` splits key from value, so
    ``path=a=b`` → ``"a=b"``; a double-quoted value may contain spaces). A bare token with no
    ``=`` after the op is ignored. Raises ``DslParseError`` if no operation token is present.
    """
    # Unbalanced double quotes are malformed → fail closed (audit r2): an unclosed quote (e.g.
    # ``path="/etc/passwd``) would otherwise tokenize as a ``\S+`` run whose retained leading ``"``
    # breaks ``is_protected_path``'s prefix match, UNDER-classifying a destructive write.
    if rest.count('"') % 2 != 0:
        raise DslParseError(
            f"unbalanced double quote in EFFECT args {rest!r} (fail-closed: a malformed quote "
            f"could corrupt a risk-bearing value)"
        )
    tokens = _RE_TOKEN.findall(rest)
    if not tokens:
        raise DslParseError("EFFECT line has no operation token")
    op = _strip_quotes(tokens[0])
    if not op:
        raise DslParseError("EFFECT line has an empty operation token")
    args: dict[str, object] = {}
    for tok in tokens[1:]:
        # A non-op arg MUST be a key=value pair. A bare / positional token (e.g.
        # ``EFFECT write_file /etc/passwd``) is REJECTED fail-closed — silently dropping it would
        # discard a risk-bearing value and let the effect UNDER-classify (audit r1). The agent must
        # write ``path=/etc/passwd`` so G1 sees the path.
        if "=" not in tok:
            raise DslParseError(
                f"EFFECT arg {tok!r} is not a key=value pair (a bare/positional arg is rejected "
                f"fail-closed so it cannot silently drop a risk-bearing value)"
            )
        key, _, val = tok.partition("=")
        if not key:
            raise DslParseError(f"EFFECT arg {tok!r} has an empty key")
        args[key] = _strip_quotes(val)
    return op, (args or None)


# ─── Public API: the constraint grammar ──────────────────────────────────────────

def parse_constraint(text: str) -> DslConstraint:
    """Parse a DSL constraint source string into a :class:`DslConstraint`.

    Grammar (line-based, deterministic; fail-closed):
        * blank lines and ``#``-comment lines are ignored.
        * ``ALLOW <tier>`` — the declared ceiling; EXACTLY ONE required (0 or >1 →
          ``DslParseError``). ``<tier>`` is a canonical label or an enum name (case-insensitive).
        * ``EFFECT <op> [k=v ...]`` — one declared effect (zero or more lines).
        * any other leading directive → ``DslParseError``.

    Zero ``EFFECT`` lines is valid syntax (it parses); it is rejected at compile time as
    ``NO_DECLARATION``.

    Raises:
        DslParseError: if the source is malformed.
    """
    ceiling: Tier | None = None
    effects: list[EffectDecl] = []

    # Normalize line endings so a CRLF/CR source parses identically to LF (audit r1: defensive —
    # a trailing ``\r`` is already ``\s`` and consumed by the anchored patterns, but normalize to
    # remove all doubt).
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _RE_COMMENT_OR_BLANK.match(line):
            continue

        m = _RE_ALLOW.match(line)
        if m:
            if ceiling is not None:
                raise DslParseError("more than one ALLOW directive")
            ceiling = _normalize_tier(m.group(1))
            continue

        m = _RE_EFFECT.match(line)
        if m:
            op, args = _tokenize_effect_line(m.group(1))
            effects.append(EffectDecl(op=op, args=args))
            continue

        raise DslParseError(f"unrecognised directive: {line.strip()!r}")

    if ceiling is None:
        raise DslParseError("no ALLOW directive found")

    return DslConstraint(ceiling=ceiling, effects=tuple(effects), raw=text)


# ─── Public API: the compile-against-tiers checker ────────────────────────────────

def compile_constraint(
    constraint: DslConstraint,
    *,
    scope: "permissions.Scope | None" = None,
) -> CompileResult:
    """Compile a parsed constraint against the G1 tier table. Deterministic; **never raises**.

    Every declared effect's tier comes from ``core.gui.tiers.resolve_tier`` (the SAME §2.7 table:
    delete/pay/send arg-aware rules, protected-path escalation, dangerous-token heuristic). The
    DSL carries no tier table of its own.

    Rules (first match returns):
        1. no declared effects → ``NO_DECLARATION`` (an opaque plan can't be certified with zero
           declared effects — fail closed).
        2. any effect classifies Full-Access → ``REJECTED`` (``requires_human=True``) — Full-Access
           NEVER self-certifies, regardless of the declared ceiling (mirrors G1's ``route_action``).
        3. the compiled tier exceeds the declared ceiling → ``REJECTED``.
        4. otherwise → ``COMPILED``.
    """
    if not constraint.effects:
        return CompileResult(
            status=CompileStatus.NO_DECLARATION,
            compiles=False,
            compiled_tier=Tier.READ_ONLY,
            ceiling=constraint.ceiling,
            per_effect=(),
            requires_human=False,
            reason="opaque plan declares no effects (fail closed)",
        )

    per_effect: list[tuple[str, Tier]] = []
    compiled_tier = Tier.READ_ONLY
    for e in constraint.effects:
        t = resolve_tier(e.op, args=e.args, scope=scope)
        per_effect.append((e.op, t))
        if t > compiled_tier:
            compiled_tier = t
    per_effect_t = tuple(per_effect)

    # Full-Access never self-certifies — route to the human interrupt regardless of the ceiling.
    if requires_human(compiled_tier):
        first_fa = next((op for op, t in per_effect if t is Tier.FULL_ACCESS), "")
        return CompileResult(
            status=CompileStatus.REJECTED,
            compiles=False,
            compiled_tier=compiled_tier,
            ceiling=constraint.ceiling,
            per_effect=per_effect_t,
            requires_human=True,
            reason=f"declared effect {first_fa!r} is Full-Access -> human interrupt",
        )

    if compiled_tier > constraint.ceiling:
        return CompileResult(
            status=CompileStatus.REJECTED,
            compiles=False,
            compiled_tier=compiled_tier,
            ceiling=constraint.ceiling,
            per_effect=per_effect_t,
            requires_human=False,
            reason=(
                f"compiled tier {compiled_tier.label} exceeds declared ceiling "
                f"{constraint.ceiling.label}"
            ),
        )

    return CompileResult(
        status=CompileStatus.COMPILED,
        compiles=True,
        compiled_tier=compiled_tier,
        ceiling=constraint.ceiling,
        per_effect=per_effect_t,
        requires_human=False,
        reason=f"compiled within ceiling {constraint.ceiling.label}",
    )


def compile_source(
    text: str,
    *,
    scope: "permissions.Scope | None" = None,
) -> CompileResult:
    """Parse then compile a DSL source string. A parse error → a ``PARSE_ERROR`` result
    (``compiles=False``) — **never raises** (a malformed declaration is a fail-closed non-compile,
    not a crash)."""
    try:
        constraint = parse_constraint(text)
    except DslParseError as e:
        return CompileResult(
            status=CompileStatus.PARSE_ERROR,
            compiles=False,
            compiled_tier=Tier.READ_ONLY,
            ceiling=Tier.READ_ONLY,
            per_effect=(),
            requires_human=False,
            reason=f"parse error: {e}",
        )
    return compile_constraint(constraint, scope=scope)


# ─── Public API: the no-over-reach guard ──────────────────────────────────────────

def requires_dsl(
    plan: ScriptPlan,
    *,
    scope: "permissions.Scope | None" = None,
) -> bool:
    """Whether the plan needs the DSL ceiling at all.

    ``True`` iff the plan is opaque AND **not** already classified Full-Access by the deterministic
    G1 table (via ``classify_script``): a script body is always opaque, so a non-Full-Access script
    needs the DSL ceiling. ``False`` iff the declared actions ALREADY carry the risk signal
    (``delete /etc/passwd``, ``pay $10``) — the deterministic G1/G6 interrupt owns it and the DSL
    must NOT over-reach.
    """
    return not classify_script(plan, scope=scope).requires_human


# ─── Public API: the G6 escape-hatch adapter ──────────────────────────────────────

def make_dsl_compiler(
    constraint_for: "Callable[[ScriptPlan], str | DslConstraint | None]",
    *,
    scope: "permissions.Scope | None" = None,
) -> "Callable[[ScriptPlan], bool]":
    """Produce a ``dsl_compile(plan) -> bool`` callable for the landed G6
    ``execute_via_escape(..., dsl_compile=...)`` seam (it returns ``False`` ⇒ ``HUMAN_INTERRUPT``
    before the runner is ever called).

    ``constraint_for`` maps a plan to its declared DSL constraint (a source string, a parsed
    :class:`DslConstraint`, or ``None`` for nothing declared). The returned callable, in order:
        1. ``not requires_dsl(plan)`` → **return True** (no over-reach: defer to G6's
           ``classify_script`` interrupt; ``constraint_for`` is NOT consulted).
        2. ``constraint_for(plan) is None`` → **return False** (opaque plan, no declaration → fail
           closed → human interrupt).
        3. else compile the declaration and **return its** ``.compiles``.
    """

    def _dsl_compile(plan: ScriptPlan) -> bool:
        # TOTAL + fail-closed: ``execute_via_escape`` calls this pre-run, so any failure (a raising
        # ``constraint_for`` callback, a non-str/non-DslConstraint declaration, any compile error)
        # must block execution, never propagate or auto-pass. KeyboardInterrupt/SystemExit propagate
        # (Exception, not BaseException — mirrors the G7 seam discipline). (audit r1.)
        try:
            # No over-reach: a plan the deterministic table already classifies is owned by G1/G6.
            if not requires_dsl(plan, scope=scope):
                return True

            decl = constraint_for(plan)
            if decl is None:
                return False  # opaque plan with no declaration → fail closed

            result = (
                compile_source(decl, scope=scope)
                if isinstance(decl, str)
                else compile_constraint(decl, scope=scope)
            )
            return result.compiles
        except Exception:  # noqa: BLE001 — fail-closed: a broken declaration callback blocks exec
            return False

    return _dsl_compile
