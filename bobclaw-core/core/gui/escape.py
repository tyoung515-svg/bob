"""
core/gui/escape.py — MS2-G6 code/CLI escape hatch (CoAct-1).

A SECOND, **manager-selected** worker modality for the GUI lane (DESIGN-MS-D1 §3-G6): for a
subgoal solvable by a small **script**, prefer the script over a brittle GUI click sequence
(~10 vs ~15 steps OSWorld [PV]) and run it inside the **locked-down Docker sandbox**. The
script's declared actions still **tier-classify through G1** (``core/gui/tiers.py``): a declared
Full-Access action raises the §2.7 human interrupt and the script never runs. Because a script
is the prime **opaque-side-effect** surface, a clearly-marked **G8 DSL-ceiling seam**
(``dsl_compile`` / ``require_declared``) is left in place — G8 (the DSL compile) is a LATER
sprint and is NOT built here.

Composition, not duplication:
  * containment = ``core/build/sandbox.py`` (the proven, Docker-verified runner: ``--network
    none``, RO ``/work`` mount, caps dropped, ``--rm`` — reused byte-for-byte, NOT modified);
  * tiering     = ``core/gui/tiers.py`` (``resolve_tier`` / ``requires_human``);
  * Tier-2 critic = ``core/verify/postcondition.make_postcondition_verifier`` (decorrelated
    cross-family, fail-safe).

Import-light: ``core.build.sandbox``, ``core.config`` and ``core.verify.postcondition`` are LAZY
imports inside the runner / factories, so importing this module pulls in no sandbox/config/
critic/backend/node/HTTP module, makes no model call, and does no I/O at import.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.gui.tiers import Tier, requires_human, resolve_tier
from core.gui.types import Action, Subgoal, Verdict

if TYPE_CHECKING:  # ``Scope`` is referenced only in string-quoted annotations — no runtime import
    import core.permissions as permissions


# ─── Enums ─────────────────────────────────────────────────────────────────────

class Modality(str, Enum):
    """The two worker modalities the escape hatch can select."""

    SCRIPT = "script"
    CLICK_SEQUENCE = "click_sequence"


class EscapeStatus(str, Enum):
    """Terminal status of an escape-hatch execution."""

    DELEGATE_CLICKS = "delegate_clicks"   # selector chose the GUI click path (a)
    HUMAN_INTERRUPT = "human_interrupt"   # G1 Full-Access (or DSL-fail) → blocked PRE-run (c)
    RAN = "ran"                           # script ran; no post-condition was checked
    VERIFIED = "verified"                 # ran AND the post-condition holds (d)
    FAILED_VERIFY = "failed_verify"       # ran but the post-condition failed (d)


# ─── Frozen dataclasses ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One declared operation a script will perform — the G1 unit for tiering.

    Compared by value; not intended for hashing (``args`` may be a dict).
    """

    name: str
    args: "Mapping[str, object] | None" = None


@dataclass(frozen=True, slots=True)
class ScriptPlan:
    """The SCRIPT modality candidate: a script body + its declared actions.

    ``code`` is written into the sandbox workspace; ``[interpreter, filename]`` is how it is
    invoked inside the container; ``actions`` are the declared ops each G1-tier-classifies;
    ``est_steps`` is the estimated step count (a script ~ few steps).
    """

    code: str
    actions: tuple[ActionSpec, ...] = ()
    interpreter: str = "python"
    filename: str = "escape_script.py"
    est_steps: int = 1


@dataclass(frozen=True, slots=True)
class ClickSequence:
    """The brittle GUI-click modality candidate (more steps). ``est_steps`` 0 ⇒ ``len(actions)``."""

    actions: tuple[Action, ...] = ()
    est_steps: int = 0


@dataclass(frozen=True, slots=True)
class ModalityChoice:
    """Result of the deterministic script-vs-click selection."""

    modality: Modality
    reason: str


@dataclass(frozen=True, slots=True)
class ScriptTierReport:
    """Result of classifying a script's declared actions through G1."""

    max_tier: Tier
    per_action: tuple[tuple[str, Tier], ...]   # (action name, classified tier) in declared order
    requires_human: bool


@dataclass(frozen=True, slots=True)
class ScriptRun:
    """Outcome of running a script in the sandbox."""

    returncode: int
    stdout: str
    stderr: str
    mode: str                  # "docker" | "subprocess" (the resolved sandbox mode)
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """True iff the script exited 0 and did not time out."""
        return self.returncode == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class ScriptPostcondition:
    """Structural (Tier-1) post-condition of a script run (Default-FAIL)."""

    expect_returncode: "int | None" = 0
    stdout_contains: tuple[str, ...] = ()
    stdout_absent: tuple[str, ...] = ()
    stderr_absent: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EscapeOutcome:
    """Complete outcome of the escape-hatch orchestrator."""

    status: EscapeStatus
    modality: Modality
    choice_reason: str
    tier: Tier
    tier_report: "ScriptTierReport | None"
    run: "ScriptRun | None"
    verdict: "Verdict | None"
    reason: str

    @property
    def human_interrupt(self) -> bool:
        """True iff the run was blocked pre-actuation awaiting a human (§2.7 interrupt)."""
        return self.status is EscapeStatus.HUMAN_INTERRUPT


# ─── Modality selection (ready-gate a) ────────────────────────────────────────────

def select_modality(
    script: "ScriptPlan | None",
    click: "ClickSequence | None",
    *,
    prefer_script_margin: int = 0,
) -> ModalityChoice:
    """Deterministically choose between the script and the click modality (no model).

    Rules (first match):
        * both ``None`` → ``CLICK_SEQUENCE`` (the GUI floor; nothing to escape to)
        * script only   → ``SCRIPT``
        * click only    → ``CLICK_SEQUENCE``
        * both → ``SCRIPT`` iff ``script.est_steps <= click_effective + prefer_script_margin``
          else ``CLICK_SEQUENCE``. A TIE favours the script (less brittle).

    ``click_effective = click.est_steps if click.est_steps > 0 else len(click.actions)``.
    """
    click_effective = 0
    if click is not None:
        click_effective = click.est_steps if click.est_steps > 0 else len(click.actions)

    if script is None and click is None:
        return ModalityChoice(Modality.CLICK_SEQUENCE, "no modality available → click floor")
    if script is not None and click is None:
        return ModalityChoice(Modality.SCRIPT, "script only")
    if script is None and click is not None:
        return ModalityChoice(Modality.CLICK_SEQUENCE, "click only")

    # Both present.
    assert script is not None and click is not None  # narrowing for type-checkers
    if script.est_steps <= click_effective + prefer_script_margin:
        return ModalityChoice(
            Modality.SCRIPT,
            f"script steps {script.est_steps} <= click steps {click_effective} "
            f"+ margin {prefer_script_margin}",
        )
    return ModalityChoice(
        Modality.CLICK_SEQUENCE,
        f"script steps {script.est_steps} > click steps {click_effective} "
        f"+ margin {prefer_script_margin}",
    )


# ─── G1 tier classification of a script (ready-gate c) ────────────────────────────

def classify_script(
    plan: ScriptPlan,
    *,
    scope: "permissions.Scope | None" = None,
    require_declared: bool = False,
) -> ScriptTierReport:
    """Tier-classify every declared action in *plan* through G1 (``resolve_tier``).

    ``max_tier`` = the highest declared tier; ``requires_human`` = it is ``Tier.FULL_ACCESS``.
    Empty ``actions`` → the deterministic floor ``Tier.WRITE_LOCAL`` (a script executes code, a
    local mutation, but runs CONTAINED in the sandbox — not intrinsically Full-Access). The G8
    seam: ``require_declared=True`` fails an empty/undeclared script CLOSED to ``FULL_ACCESS``
    (the future DSL ceiling). Never raises (``resolve_tier`` is total).
    """
    if not plan.actions:
        max_tier = Tier.FULL_ACCESS if require_declared else Tier.WRITE_LOCAL
        return ScriptTierReport(
            max_tier=max_tier,
            per_action=(),
            requires_human=requires_human(max_tier),
        )

    per_action: list[tuple[str, Tier]] = []
    max_tier = Tier.READ_ONLY  # start low; raised by the highest declared tier
    for spec in plan.actions:
        t = resolve_tier(spec.name, args=spec.args, scope=scope)
        per_action.append((spec.name, t))
        if t > max_tier:
            max_tier = t

    return ScriptTierReport(
        max_tier=max_tier,
        per_action=tuple(per_action),
        requires_human=requires_human(max_tier),
    )


# ─── Sandbox runner (ready-gate b) ────────────────────────────────────────────────

class SandboxRunner(Protocol):
    """The unit-test mock seam for the containment runner."""

    def run(self, workspace: "Path", argv: "list[str]", *, timeout: int) -> ScriptRun: ...


class DockerSandboxRunner:
    """Default runner — composes ``core/build/sandbox.py`` (lazy) WITHOUT modifying it.

    Resolves the mode via ``sandbox.resolve_mode()``: ``docker`` runs ``argv`` inside the
    hardened container via the proven ``sandbox._run`` (``sandbox._docker_argv``: ``--network
    none``, ``-v ws:/work:ro``, ``--cap-drop ALL``, ``--read-only`` + ``/tmp`` tmpfs, caps,
    ``--rm``, reap-on-timeout). ``subprocess`` (Docker absent) runs untrusted LLM code, so it
    is REFUSED with ``SandboxUnavailable`` unless ``allow_host=True`` (the trusted-CI escape,
    mirroring ``sandbox.py``'s own mode contract) — then host ``subprocess.run`` (un-contained;
    it INHERITS the host env so a bare interpreter resolves, Docker being the real boundary).
    ``.run`` never raises out of a timeout (→ ``ScriptRun(timed_out=True)``) in EITHER mode.
    """

    def __init__(self, *, allow_host: bool = False) -> None:
        self._allow_host = allow_host

    def run(self, workspace: "Path", argv: "list[str]", *, timeout: int) -> ScriptRun:
        from core.build import sandbox  # lazy: keep escape.py import-light

        mode = sandbox.resolve_mode()
        if mode == "docker":
            # ``sandbox._run`` reaps the container then RE-RAISES TimeoutExpired — map it to a
            # fail-safe ``ScriptRun`` so ``.run`` is total on a timeout in BOTH modes (audit r1:
            # the docker branch previously let a timed-out run escape as a raw exception). Other
            # (infra) errors from ``sandbox._run`` PROPAGATE by design — like ``SandboxUnavailable``,
            # a broken container runtime is surfaced, never masked as a "script failed" result.
            try:
                proc = sandbox._run(Path(workspace), list(argv), timeout)
            except subprocess.TimeoutExpired as e:
                return ScriptRun(
                    returncode=124,
                    stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                    stderr="timeout",
                    mode="docker",
                    timed_out=True,
                )
            return ScriptRun(
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                mode="docker",
            )

        # mode == "subprocess" — untrusted code on the host → refuse unless explicitly allowed.
        if not self._allow_host:
            raise sandbox.SandboxUnavailable(
                "escape hatch requires the Docker sandbox; refusing to run untrusted code "
                "un-contained (set allow_host=True only for trusted CI)"
            )
        try:
            # Un-contained trusted-CI path: INHERIT the host env (so a bare interpreter like
            # "python" resolves via PATH — and, on Windows, SystemRoot is present for socket/ssl
            # init), then overlay the workspace PYTHONPATH. Docker is the real containment; the
            # host path never claimed to be a boundary (audit r6: a stripped env broke execution).
            child_env = {
                **os.environ,
                "PYTHONPATH": str(workspace),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            proc = subprocess.run(
                list(argv),
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env,
            )
        except subprocess.TimeoutExpired as e:
            return ScriptRun(
                returncode=124,
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr="timeout",
                mode="subprocess",
                timed_out=True,
            )
        return ScriptRun(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            mode="subprocess",
        )


def run_script_in_sandbox(
    plan: ScriptPlan,
    workspace: "Path",
    *,
    runner: "SandboxRunner | None" = None,
    timeout: "int | None" = None,
) -> ScriptRun:
    """Write *plan.code* into *workspace* and run it via the sandbox *runner*.

    Rejects a ``plan.filename`` containing a path separator or ``..`` (the script must land in
    the workspace root, the only thing the container RO-mounts). ``timeout`` defaults to
    ``core.config.BUILD_VERIFY_TIMEOUT`` (lazy import). Returns the runner's ``ScriptRun``.
    """
    name = plan.filename
    if (not name or name in (".", "..")
            or any(sep in name for sep in (os.sep, "/", "\\")) or ".." in name):
        raise ValueError(
            f"script filename {name!r} must be a bare filename (no separator, no '..') — refusing"
        )
    # Belt: the RESOLVED destination must sit DIRECTLY in the workspace root — rejects an
    # absolute / drive-relative filename that would discard the workspace and escape the
    # RO-mounted /work (audit r1: a bare "C:foo"/absolute path could slip past the string check).
    ws_resolved = Path(workspace).resolve()
    dst = (ws_resolved / name).resolve()
    if dst.parent != ws_resolved:
        raise ValueError(
            f"script filename {name!r} does not resolve inside the workspace root — refusing"
        )
    dst.write_text(plan.code, encoding="utf-8")

    if timeout is None:
        from core.config import BUILD_VERIFY_TIMEOUT  # lazy: keep import-light
        timeout = BUILD_VERIFY_TIMEOUT

    # Pass the SAME resolved workspace to the runner that we wrote into (audit r4): the docker
    # mount + the host cwd then match the write target exactly, with no relative/symlink drift.
    runner = runner or DockerSandboxRunner()
    return runner.run(ws_resolved, [plan.interpreter, plan.filename], timeout=timeout)


# ─── Post-condition verification (ready-gate d) ────────────────────────────────────

def verify_script_run(run: ScriptRun, pc: ScriptPostcondition) -> Verdict:
    """Deterministic Default-FAIL Tier-1 floor for a script run's post-condition.

    Each criterion starts false and flips true only on positive evidence (returncode match;
    every ``stdout_contains`` present; every ``stdout_absent`` / ``stderr_absent`` genuinely
    absent). Empty criteria → ``Verdict(ok=False, reason="no postcondition criteria")``
    (never trivially passes). Mirrors ``core/gui/verify.verify_postcondition``.
    """
    criteria: list[tuple[str, bool]] = []

    if pc.expect_returncode is not None:
        criteria.append(("returncode", run.returncode == pc.expect_returncode))

    for s in pc.stdout_contains:
        criteria.append((f"stdout_contains:{s}", bool(s) and s in run.stdout))

    for s in pc.stdout_absent:
        criteria.append((f"stdout_absent:{s}", bool(s) and s not in run.stdout))

    for s in pc.stderr_absent:
        criteria.append((f"stderr_absent:{s}", bool(s) and s not in run.stderr))

    if not criteria:
        return Verdict(ok=False, reason="no postcondition criteria", criteria=())

    if all(v for _, v in criteria):
        return Verdict(ok=True, reason="all criteria satisfied", criteria=tuple(criteria))

    fail_name = next((name for name, value in criteria if not value), "")
    return Verdict(ok=False, reason=f"failed: {fail_name}", criteria=tuple(criteria))


def make_script_semantic_verifier(
    *,
    actor_backend: str,
    team: "str | None" = None,
    critic_backend: "str | None" = None,
    send=None,
    render_run: "Callable[[ScriptRun], str] | None" = None,
) -> "Callable[[Subgoal, ScriptRun], Verdict]":
    """Tier-2 escalation: reuse the MS-2 decorrelated cross-family critic for a script run's
    SEMANTIC post-condition the structural floor can't judge.

    Reuses ``core.verify.postcondition.make_postcondition_verifier`` (lazy import) — it does NOT
    re-implement the critic or the async bridge. The returned ``verify(subgoal, run) -> Verdict``
    builds the MS-2 payload and maps pass/fail to a ``Verdict``. Fail-safe: violated / unknown /
    unreachable / any error → ``ok=False`` (a non-decorrelated config can never deceptively
    pass). Decorrelation is enforced inside MS-2. POST-run, off any critical path.
    """
    from core.verify.postcondition import make_postcondition_verifier  # lazy: keep import-light

    _bool = make_postcondition_verifier(team=team, critic_backend=critic_backend, send=send)
    _render = render_run or (
        lambda r: f"returncode={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )

    def verify(subgoal: Subgoal, run: ScriptRun) -> Verdict:
        payload = {
            "step": f"script-run rc={run.returncode}",
            "statement": subgoal.text,
            "result": _render(run),
            "actor_backend": actor_backend,
        }
        try:
            passed = _bool(payload)
        except Exception:  # noqa: BLE001 — fail-safe: any unexpected error never auto-passes
            passed = False
        reason = "tier2-script: holds" if passed else "tier2-script: not-satisfied (fail-safe)"
        return Verdict(ok=passed, reason=reason, criteria=(("tier2-script", passed),))

    return verify


# ─── Orchestrator (composes a + c + b + d) ─────────────────────────────────────────

def execute_via_escape(
    subgoal: Subgoal,
    *,
    script: "ScriptPlan | None",
    click: "ClickSequence | None" = None,
    workspace: "Path",
    scope: "permissions.Scope | None" = None,
    runner: "SandboxRunner | None" = None,
    postcondition: "ScriptPostcondition | None" = None,
    semantic_verifier: "Callable[[Subgoal, ScriptRun], Verdict] | None" = None,
    prefer_script_margin: int = 0,
    require_declared: bool = False,
    dsl_compile: "Callable[[ScriptPlan], bool] | None" = None,
) -> EscapeOutcome:
    """The CoAct-1 escape-hatch orchestrator: select → (G8 seam) → tier → run → verify.

    Steps (first terminal wins):
        1. ``select_modality`` — CLICK_SEQUENCE (or ``script is None``) → ``DELEGATE_CLICKS``
           (the GUI loop owns the click path; the escape hatch steps aside).
        2. **G8 seam** — ``dsl_compile`` is not None and returns False → ``HUMAN_INTERRUPT``
           (the opaque plan did not compile against the tiers; do NOT run). Default None ⇒ no-op.
        3. ``classify_script`` — ``requires_human`` → ``HUMAN_INTERRUPT`` (the declared
           Full-Access script NEVER runs; the runner is not called, ``run is None``).
        4. ``run_script_in_sandbox`` — real Docker via the default runner (``SandboxUnavailable``
           propagates; containment is mandatory).
        5. **Verify** — Tier-1 ``verify_script_run`` floor; if the floor yields no judgeable
           criteria AND a ``semantic_verifier`` is wired → escalate (Tier-2, fail-safe). status
           ``VERIFIED`` / ``FAILED_VERIFY`` / ``RAN``.
    """
    choice = select_modality(script, click, prefer_script_margin=prefer_script_margin)
    if choice.modality is Modality.CLICK_SEQUENCE:
        return EscapeOutcome(
            status=EscapeStatus.DELEGATE_CLICKS,
            modality=Modality.CLICK_SEQUENCE,
            choice_reason=choice.reason,
            tier=Tier.READ_ONLY,  # the click sequence is not classified here (G3/G5 own it)
            tier_report=None,
            run=None,
            verdict=None,
            reason=f"click path chosen: {choice.reason}",
        )

    # Modality is SCRIPT ⇒ script is not None.
    assert script is not None

    # 2. G8 DSL-ceiling seam (not built — the hook is left for a later sprint).
    if dsl_compile is not None and not dsl_compile(script):
        return EscapeOutcome(
            status=EscapeStatus.HUMAN_INTERRUPT,
            modality=Modality.SCRIPT,
            choice_reason=choice.reason,
            tier=Tier.FULL_ACCESS,
            tier_report=None,
            run=None,
            verdict=None,
            reason="opaque plan did not compile against the tiers (G8 DSL seam)",
        )

    # 3. G1 tier-classification — a declared Full-Access action blocks PRE-run.
    tier_report = classify_script(script, scope=scope, require_declared=require_declared)
    if tier_report.requires_human:
        return EscapeOutcome(
            status=EscapeStatus.HUMAN_INTERRUPT,
            modality=Modality.SCRIPT,
            choice_reason=choice.reason,
            tier=Tier.FULL_ACCESS,
            tier_report=tier_report,
            run=None,
            verdict=None,
            reason=(
                "script declares a Full-Access action; blocked pre-run "
                f"(max tier: {tier_report.max_tier.label})"
            ),
        )

    # 4. Run inside the (real Docker) sandbox.
    run = run_script_in_sandbox(script, workspace, runner=runner)

    # 5. Two-tier post-condition verification.
    verdict: "Verdict | None" = None
    if postcondition is not None:
        verdict = verify_script_run(run, postcondition)
        # Floor produced NO judgeable criteria → escalate to the Tier-2 critic when wired.
        if not verdict.criteria and semantic_verifier is not None:
            verdict = semantic_verifier(subgoal, run)
    elif semantic_verifier is not None:
        verdict = semantic_verifier(subgoal, run)

    if verdict is None:
        status, reason = EscapeStatus.RAN, "script ran; no post-condition verification wired"
    elif verdict.ok:
        status, reason = EscapeStatus.VERIFIED, verdict.reason
    else:
        status, reason = EscapeStatus.FAILED_VERIFY, verdict.reason

    return EscapeOutcome(
        status=status,
        modality=Modality.SCRIPT,
        choice_reason=choice.reason,
        tier=tier_report.max_tier,
        tier_report=tier_report,
        run=run,
        verdict=verdict,
        reason=reason,
    )
