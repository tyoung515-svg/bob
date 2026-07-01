"""Tests for core.gui.escape (MS2-G6 code/CLI escape hatch, CoAct-1).

Sandbox fully MOCKED (a fake SandboxRunner / monkeypatched core.build.sandbox) — zero Docker,
zero subprocess of untrusted code, zero model. The real Docker containment is proven separately
in live_e2e_g6.py.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from core.gui.types import Action, ActionKind, Subgoal, Verdict
from core.gui.tiers import Tier
from core.gui.escape import (
    Modality,
    ActionSpec,
    ScriptPlan,
    ClickSequence,
    ModalityChoice,
    ScriptTierReport,
    ScriptRun,
    ScriptPostcondition,
    EscapeStatus,
    EscapeOutcome,
    SandboxRunner,
    DockerSandboxRunner,
    select_modality,
    classify_script,
    run_script_in_sandbox,
    verify_script_run,
    make_script_semantic_verifier,
    execute_via_escape,
)


# ─── Fake / Spy runners ─────────────────────────────────────────────────────────

class FakeRunner:
    """Records every (workspace, argv, timeout) and returns a preset ScriptRun."""

    def __init__(self, *, script_run: ScriptRun) -> None:
        self.calls: list[tuple[Path, list[str], int]] = []
        self.script_run = script_run

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def run(self, workspace: Path, argv: list[str], *, timeout: int) -> ScriptRun:
        self.calls.append((workspace, argv, timeout))
        return self.script_run


class SpyRunner:
    """Counts calls; PROVES a blocked/delegated path never actuates a script."""

    def __init__(self) -> None:
        self.call_count = 0

    def run(self, workspace: Path, argv: list[str], *, timeout: int) -> ScriptRun:
        self.call_count += 1
        return ScriptRun(returncode=0, stdout="", stderr="", mode="spy")


def _make_fake_send(json_response: str):
    """An async send(messages, backend) -> *json_response* (the MS-2 critic transport seam)."""

    async def fake_send(messages, backend):
        return json_response

    return fake_send


# ─── select_modality (ready-gate a) ──────────────────────────────────────────────

class TestSelectModality:
    def test_script_only(self) -> None:
        choice = select_modality(ScriptPlan(code="", est_steps=5), None)
        assert choice.modality is Modality.SCRIPT

    def test_click_only(self) -> None:
        choice = select_modality(None, ClickSequence(est_steps=5))
        assert choice.modality is Modality.CLICK_SEQUENCE

    def test_both_none(self) -> None:
        choice = select_modality(None, None)
        assert choice.modality is Modality.CLICK_SEQUENCE

    def test_script_fewer_steps(self) -> None:
        choice = select_modality(ScriptPlan(code="", est_steps=10), ClickSequence(est_steps=15))
        assert choice.modality is Modality.SCRIPT

    def test_script_more_steps_no_margin(self) -> None:
        choice = select_modality(
            ScriptPlan(code="", est_steps=20), ClickSequence(est_steps=15), prefer_script_margin=0
        )
        assert choice.modality is Modality.CLICK_SEQUENCE

    def test_tie_favors_script(self) -> None:
        choice = select_modality(ScriptPlan(code="", est_steps=10), ClickSequence(est_steps=10))
        assert choice.modality is Modality.SCRIPT

    def test_margin_allows_slightly_pricier_script(self) -> None:
        choice = select_modality(
            ScriptPlan(code="", est_steps=18), ClickSequence(est_steps=15), prefer_script_margin=5
        )
        assert choice.modality is Modality.SCRIPT

    def test_click_zero_est_steps_uses_len_actions(self) -> None:
        click = ClickSequence(
            actions=(Action(kind=ActionKind.CLICK),) * 3, est_steps=0  # effective 3 steps
        )
        # script 2 <= effective click 3 → SCRIPT; script 4 > 3 → CLICK_SEQUENCE.
        assert select_modality(ScriptPlan(code="", est_steps=2), click).modality is Modality.SCRIPT
        assert (
            select_modality(ScriptPlan(code="", est_steps=4), click).modality
            is Modality.CLICK_SEQUENCE
        )


# ─── classify_script (ready-gate c) ───────────────────────────────────────────────

class TestClassifyScript:
    def test_benign_read_file(self) -> None:
        report = classify_script(
            ScriptPlan(code="", actions=(ActionSpec("read_file", {"path": "scratch/x"}),))
        )
        assert report.max_tier is Tier.READ_ONLY
        assert report.requires_human is False
        assert report.per_action == (("read_file", Tier.READ_ONLY),)

    def test_destructive_delete_protected(self) -> None:
        report = classify_script(
            ScriptPlan(code="", actions=(ActionSpec("delete", {"path": "/etc/passwd"}),))
        )
        assert report.max_tier is Tier.FULL_ACCESS
        assert report.requires_human is True

    def test_pay_nonzero_full_access(self) -> None:
        report = classify_script(
            ScriptPlan(code="", actions=(ActionSpec("pay", {"amount": 100}),))
        )
        assert report.max_tier is Tier.FULL_ACCESS
        assert report.requires_human is True

    def test_mixed_max_tier_and_order(self) -> None:
        report = classify_script(
            ScriptPlan(
                code="",
                actions=(
                    ActionSpec("read_file", {"path": "scratch/x"}),
                    ActionSpec("delete", {"path": "/etc/passwd"}),
                ),
            )
        )
        assert report.max_tier is Tier.FULL_ACCESS
        assert report.requires_human is True
        assert report.per_action[0] == ("read_file", Tier.READ_ONLY)
        assert report.per_action[1] == ("delete", Tier.FULL_ACCESS)

    def test_empty_actions_floor_write_local(self) -> None:
        report = classify_script(ScriptPlan(code="", actions=()))
        assert report.max_tier is Tier.WRITE_LOCAL
        assert report.requires_human is False
        assert report.per_action == ()

    def test_empty_actions_require_declared_fails_closed(self) -> None:
        report = classify_script(ScriptPlan(code="", actions=()), require_declared=True)
        assert report.max_tier is Tier.FULL_ACCESS
        assert report.requires_human is True


# ─── run_script_in_sandbox (fake runner) ──────────────────────────────────────────

class TestRunScriptInSandbox:
    def test_writes_script_and_delegates(self, tmp_path: Path) -> None:
        fake_run = ScriptRun(0, "", "", "fake")
        runner = FakeRunner(script_run=fake_run)
        result = run_script_in_sandbox(
            ScriptPlan(code="print(1)", filename="s.py"), tmp_path, runner=runner, timeout=7
        )
        assert result is fake_run
        assert (tmp_path / "s.py").read_text(encoding="utf-8") == "print(1)"
        assert runner.call_count == 1
        ws, argv, to = runner.calls[0]
        assert ws == tmp_path.resolve()  # the runner gets the SAME resolved ws we wrote into (audit r4)
        assert argv == ["python", "s.py"]
        assert to == 7

    def test_rejects_traversal_filename(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            run_script_in_sandbox(
                ScriptPlan(code="x", filename="../evil.py"),
                tmp_path,
                runner=FakeRunner(script_run=ScriptRun(0, "", "", "fake")),
            )
        assert not (tmp_path / ".." / "evil.py").exists()

    def test_rejects_separator_filename(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            run_script_in_sandbox(
                ScriptPlan(code="x", filename="a/b.py"),
                tmp_path,
                runner=FakeRunner(script_run=ScriptRun(0, "", "", "fake")),
            )
        assert not (tmp_path / "a").exists()

    @pytest.mark.parametrize("bad", ["/etc/passwd", "", ".", ".."])
    def test_rejects_absolute_or_degenerate_filename(self, tmp_path: Path, bad: str) -> None:
        # audit r1: an absolute / drive-relative / degenerate name must not escape the workspace root.
        with pytest.raises(ValueError):
            run_script_in_sandbox(
                ScriptPlan(code="x", filename=bad),
                tmp_path,
                runner=FakeRunner(script_run=ScriptRun(0, "", "", "fake")),
            )


# ─── DockerSandboxRunner modes (sandbox monkeypatched — no real Docker/host code) ─

class TestDockerSandboxRunner:
    def test_docker_mode_composes_sandbox_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.build.sandbox as sb

        monkeypatch.setattr(sb, "resolve_mode", lambda: "docker")
        monkeypatch.setattr(
            sb, "_run",
            lambda ws, argv, timeout: types.SimpleNamespace(returncode=0, stdout="OUT", stderr=""),
        )
        result = DockerSandboxRunner().run(Path("/tmp"), ["python", "s.py"], timeout=5)
        assert result.mode == "docker"
        assert (result.returncode, result.stdout, result.stderr) == (0, "OUT", "")
        assert result.ok is True

    def test_subprocess_mode_refused_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.build.sandbox as sb

        monkeypatch.setattr(sb, "resolve_mode", lambda: "subprocess")
        with pytest.raises(sb.SandboxUnavailable):
            DockerSandboxRunner().run(Path("/tmp"), ["python", "s.py"], timeout=5)

    def test_subprocess_mode_allowed_with_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.build.sandbox as sb
        import core.gui.escape as esc

        monkeypatch.setattr(sb, "resolve_mode", lambda: "subprocess")
        captured: dict = {}

        def _record(*a, **kw):
            captured.update(kw)
            return types.SimpleNamespace(returncode=0, stdout="OUT", stderr="")

        monkeypatch.setattr(esc.subprocess, "run", _record)
        result = DockerSandboxRunner(allow_host=True).run(Path("/tmp"), ["python", "s.py"], timeout=5)
        assert result.mode == "subprocess"
        assert result.returncode == 0
        assert result.stdout == "OUT"
        # audit r6: the host child env must INHERIT PATH so a bare "python" interpreter resolves
        # (a stripped env broke the trusted-CI host path). PYTHONPATH points at the workspace.
        assert "PATH" in captured["env"]
        assert captured["env"]["PYTHONPATH"] == str(Path("/tmp"))

    def test_timeout_is_failsafe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import core.build.sandbox as sb
        import core.gui.escape as esc

        monkeypatch.setattr(sb, "resolve_mode", lambda: "subprocess")

        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="x", timeout=5)

        monkeypatch.setattr(esc.subprocess, "run", _raise)
        result = DockerSandboxRunner(allow_host=True).run(Path("/tmp"), ["python", "s.py"], timeout=5)
        assert result.timed_out is True
        assert result.ok is False  # never raises out

    def test_docker_timeout_is_failsafe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # audit r1: sandbox._run RE-RAISES TimeoutExpired (after reaping) — the docker branch must
        # map it to ScriptRun(timed_out=True), total like the subprocess branch (never escapes).
        import core.build.sandbox as sb

        monkeypatch.setattr(sb, "resolve_mode", lambda: "docker")

        def _raise(ws, argv, timeout):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)

        monkeypatch.setattr(sb, "_run", _raise)
        result = DockerSandboxRunner().run(Path("/tmp"), ["python", "s.py"], timeout=5)
        assert result.mode == "docker"
        assert result.timed_out is True and result.ok is False  # never raises out


# ─── verify_script_run (Tier-1 floor, Default-FAIL) ───────────────────────────────

class TestVerifyScriptRun:
    def test_all_criteria_hold(self) -> None:
        v = verify_script_run(
            ScriptRun(0, "RESULT:4 done", "", "docker"),
            ScriptPostcondition(stdout_contains=("RESULT:4",)),
        )
        assert v.ok is True

    def test_returncode_mismatch_fails(self) -> None:
        v = verify_script_run(
            ScriptRun(1, "RESULT:4", "", "docker"),
            ScriptPostcondition(expect_returncode=0, stdout_contains=("RESULT:4",)),
        )
        assert v.ok is False
        assert "returncode" in v.reason

    def test_stdout_absent_present_fails(self) -> None:
        v = verify_script_run(
            ScriptRun(0, "RESULT:4 LEAKED", "", "docker"),
            ScriptPostcondition(stdout_absent=("LEAKED",)),
        )
        assert v.ok is False

    def test_stderr_absent_present_fails(self) -> None:
        v = verify_script_run(
            ScriptRun(0, "ok", "Traceback: boom", "docker"),
            ScriptPostcondition(stderr_absent=("Traceback",)),
        )
        assert v.ok is False

    def test_empty_postcondition_default_fail(self) -> None:
        v = verify_script_run(
            ScriptRun(0, "anything", "", "docker"),
            ScriptPostcondition(expect_returncode=None),
        )
        assert v.ok is False
        assert "no postcondition criteria" in v.reason


# ─── make_script_semantic_verifier (Tier-2, MS-2 reuse, fake send) ────────────────

class TestSemanticVerifier:
    def test_holds(self) -> None:
        verifier = make_script_semantic_verifier(
            actor_backend="deepseek_v4_flash", send=_make_fake_send('{"verdict":"holds","reasons":[]}')
        )
        assert verifier(Subgoal("did it"), ScriptRun(0, "ok", "", "docker")).ok is True

    def test_violated_fails_safe(self) -> None:
        verifier = make_script_semantic_verifier(
            actor_backend="deepseek_v4_flash", send=_make_fake_send('{"verdict":"violated"}')
        )
        assert verifier(Subgoal("did it"), ScriptRun(0, "ok", "", "docker")).ok is False

    def test_send_error_fails_safe(self) -> None:
        async def _raise(messages, backend):
            raise RuntimeError("send failed")

        verifier = make_script_semantic_verifier(actor_backend="deepseek_v4_flash", send=_raise)
        assert verifier(Subgoal("did it"), ScriptRun(0, "ok", "", "docker")).ok is False

    def test_same_family_critic_never_passes(self) -> None:
        # A forced SAME-family critic is not decorrelated; MS-2 fails it CLOSED at the boundary,
        # so even a 'holds' send can NEVER produce a deceptive pass (consistent with G3).
        verifier = make_script_semantic_verifier(
            actor_backend="deepseek_v4_flash",
            critic_backend="deepseek_v4_flash",  # same family "deepseek" → not decorrelated
            send=_make_fake_send('{"verdict":"holds","reasons":[]}'),
        )
        assert verifier(Subgoal("did it"), ScriptRun(0, "ok", "", "docker")).ok is False


# ─── execute_via_escape orchestrator (a + c + b + d) ──────────────────────────────

class TestExecuteViaEscape:
    def test_benign_script_runs_and_verifies(self, tmp_path: Path) -> None:
        runner = FakeRunner(script_run=ScriptRun(0, "RESULT:4", "", "docker"))
        outcome = execute_via_escape(
            Subgoal("compute"),
            script=ScriptPlan(
                code="print('RESULT:4')",
                actions=(ActionSpec("read_file", {"path": "scratch/x"}),),
                est_steps=2,
            ),
            click=ClickSequence(est_steps=9),
            workspace=tmp_path,
            runner=runner,
            postcondition=ScriptPostcondition(stdout_contains=("RESULT:4",)),
        )
        assert outcome.status is EscapeStatus.VERIFIED
        assert outcome.modality is Modality.SCRIPT
        assert outcome.run is not None and outcome.verdict is not None and outcome.verdict.ok
        assert runner.call_count == 1

    def test_destructive_declared_never_runs(self, tmp_path: Path) -> None:
        spy = SpyRunner()
        outcome = execute_via_escape(
            Subgoal("wipe"),
            script=ScriptPlan(code="rm -rf /", actions=(ActionSpec("delete", {"path": "/etc/passwd"}),)),
            workspace=tmp_path,
            runner=spy,
        )
        assert outcome.status is EscapeStatus.HUMAN_INTERRUPT
        assert outcome.human_interrupt is True
        assert outcome.tier is Tier.FULL_ACCESS
        assert outcome.run is None
        assert spy.call_count == 0  # the destructive script NEVER ran

    def test_click_chosen_delegates(self, tmp_path: Path) -> None:
        spy = SpyRunner()
        outcome = execute_via_escape(
            Subgoal("scroll"),
            script=None,
            click=ClickSequence(est_steps=3, actions=(Action(kind=ActionKind.CLICK),)),
            workspace=tmp_path,
            runner=spy,
        )
        assert outcome.status is EscapeStatus.DELEGATE_CLICKS
        assert outcome.modality is Modality.CLICK_SEQUENCE
        assert outcome.run is None
        assert spy.call_count == 0

    def test_postcondition_failure(self, tmp_path: Path) -> None:
        runner = FakeRunner(script_run=ScriptRun(0, "WRONG", "", "docker"))
        outcome = execute_via_escape(
            Subgoal("compute"),
            script=ScriptPlan(code="print('WRONG')", actions=(ActionSpec("read_file", {"path": "scratch/x"}),), est_steps=2),
            workspace=tmp_path,
            runner=runner,
            postcondition=ScriptPostcondition(stdout_contains=("RESULT:4",)),
        )
        assert outcome.status is EscapeStatus.FAILED_VERIFY
        assert outcome.run is not None
        assert runner.call_count == 1

    def test_dsl_compile_false_interrupts(self, tmp_path: Path) -> None:
        spy = SpyRunner()
        outcome = execute_via_escape(
            Subgoal("opaque"),
            script=ScriptPlan(code="print('x')", actions=(), est_steps=1),
            workspace=tmp_path,
            runner=spy,
            dsl_compile=lambda plan: False,
        )
        assert outcome.status is EscapeStatus.HUMAN_INTERRUPT
        assert outcome.run is None
        assert spy.call_count == 0

    def test_tier2_escalation_when_floor_has_no_criteria(self, tmp_path: Path) -> None:
        runner = FakeRunner(script_run=ScriptRun(0, "x", "", "docker"))
        invoked: list = []

        def semantic_verifier(subgoal, run):
            invoked.append((subgoal, run))
            return Verdict(ok=True, reason="seal")

        outcome = execute_via_escape(
            Subgoal("semantic"),
            script=ScriptPlan(code="print('x')", actions=(), est_steps=1),
            workspace=tmp_path,
            runner=runner,
            postcondition=ScriptPostcondition(expect_returncode=None),  # no judgeable criteria
            semantic_verifier=semantic_verifier,
        )
        assert outcome.status is EscapeStatus.VERIFIED
        assert outcome.verdict is not None and "seal" in outcome.verdict.reason
        assert len(invoked) == 1
        assert runner.call_count == 1

    def test_sandbox_unavailable_propagates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # audit r1: containment is mandatory — with Docker absent and the DEFAULT runner (allow_host
        # False), execute_via_escape must let SandboxUnavailable PROPAGATE, never silently run
        # untrusted code un-contained.
        import core.build.sandbox as sb

        monkeypatch.setattr(sb, "resolve_mode", lambda: "subprocess")
        with pytest.raises(sb.SandboxUnavailable):
            execute_via_escape(
                Subgoal("benign"),
                script=ScriptPlan(code="print('x')", actions=(ActionSpec("read_file", {"path": "scratch/x"}),)),
                workspace=tmp_path,  # default runner=None → DockerSandboxRunner(allow_host=False)
            )

    def test_ran_status_when_no_verifier(self, tmp_path: Path) -> None:
        runner = FakeRunner(script_run=ScriptRun(0, "x", "", "docker"))
        outcome = execute_via_escape(
            Subgoal("noverify"),
            script=ScriptPlan(code="print('x')", actions=(ActionSpec("read_file", {"path": "scratch/x"}),)),
            workspace=tmp_path,
            runner=runner,
        )
        assert outcome.status is EscapeStatus.RAN
        assert outcome.run is not None and outcome.verdict is None

    def test_tier1_failure_not_overridden_by_tier2(self, tmp_path: Path) -> None:
        # audit r3: a Tier-1 floor that produced criteria and FAILED is authoritative — the Tier-2
        # semantic verifier must NOT be consulted (escalation fires only on an EMPTY floor), so a
        # script that fails its declared post-condition can never be flipped to VERIFIED.
        runner = FakeRunner(script_run=ScriptRun(0, "WRONG", "", "docker"))
        invoked: list = []

        def semantic_verifier(subgoal, run):
            invoked.append(run)
            return Verdict(ok=True, reason="should-not-be-used")

        outcome = execute_via_escape(
            Subgoal("compute"),
            script=ScriptPlan(code="print('WRONG')", actions=(ActionSpec("read_file", {"path": "scratch/x"}),), est_steps=2),
            workspace=tmp_path,
            runner=runner,
            postcondition=ScriptPostcondition(stdout_contains=("RESULT:4",)),  # has criteria, fails
            semantic_verifier=semantic_verifier,
        )
        assert outcome.status is EscapeStatus.FAILED_VERIFY
        assert invoked == []  # Tier-2 NOT consulted after a Tier-1 floor failure

    def test_runner_infra_error_propagates(self, tmp_path: Path) -> None:
        # audit r3: an unexpected runner/infra error (NOT SandboxUnavailable) SURFACES — it is never
        # masked as a script-level FAILED/VERIFIED (which would be a deceptive result). Mirrors the
        # SandboxUnavailable propagation stance: containment/infra failures fail closed by surfacing.
        class BrokenRunner:
            def run(self, workspace, argv, *, timeout):
                raise RuntimeError("docker daemon vanished mid-run")

        with pytest.raises(RuntimeError):
            execute_via_escape(
                Subgoal("compute"),
                script=ScriptPlan(code="print('x')", actions=(ActionSpec("read_file", {"path": "scratch/x"}),)),
                workspace=tmp_path,
                runner=BrokenRunner(),
            )


# ─── import purity / scope fence ──────────────────────────────────────────────────

class TestImportPurity:
    def test_source_has_no_forbidden_imports(self) -> None:
        src = Path(__file__).resolve().parents[2] / "core" / "gui" / "escape.py"
        text = src.read_text(encoding="utf-8")
        for kw in ("core.backends", "core.nodes", "aiohttp", "requests", "httpx", "import docker"):
            assert kw not in text, f"forbidden import found: {kw}"

    def test_import_is_lazy(self) -> None:
        root = Path(__file__).resolve().parents[2]  # bobclaw-core
        code = (
            "import core.gui.escape, sys; "
            "print(any(m == 'core.build.sandbox' or m == 'core.verify.postcondition' "
            "or m == 'core.backends' or m == 'core.nodes' for m in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False", f"lazy modules eagerly loaded: {proc.stdout}"
