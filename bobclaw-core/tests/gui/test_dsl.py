"""MS2-G8 — unit tests for the DSL intent-formalization ceiling (core/gui/dsl.py).

PURE: zero model, zero Docker, zero network. The G6 integration uses an INJECTED spy runner
(no real sandbox). Run from bobclaw-core/ with PYTHONPATH=.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from core.gui.tiers import Tier
from core.gui.escape import (
    ScriptPlan,
    ActionSpec,
    execute_via_escape,
    EscapeStatus,
    ScriptRun,
)
from core.gui.types import Subgoal
from core.permissions import Scope
from core.gui.dsl import (
    DslParseError,
    DslConstraint,
    CompileStatus,
    parse_constraint,
    compile_constraint,
    compile_source,
    requires_dsl,
    make_dsl_compiler,
)


# ---------------------------------------------------------------------------
# Helper classes / fixtures
# ---------------------------------------------------------------------------

class RecordingRunner:
    """Spy runner that records its call count and returns a successful docker ScriptRun."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, workspace: Path, argv: list[str], *, timeout: int) -> ScriptRun:
        self.calls += 1
        return ScriptRun(returncode=0, stdout="ok", stderr="", mode="docker")


class RecordingConstraintFor:
    """Spy ``constraint_for`` callable: records call count and returns a fixed constraint."""

    def __init__(self, constraint: "str | None" = None) -> None:
        self.calls = 0
        self.constraint = constraint

    def __call__(self, plan: ScriptPlan) -> "str | None":
        self.calls += 1
        return self.constraint


# A benign opaque script (declared actions benign; the body is opaque).
benign_plan = ScriptPlan(
    code="print('hi')",
    actions=(ActionSpec("read_file", {"path": "scratch/in.txt"}),),
    est_steps=1,
)

# A plan whose declared actions ALREADY classify Full-Access (the deterministic table owns it).
full_access_plan = ScriptPlan(
    code="x",
    actions=(ActionSpec("delete", {"path": "/etc/passwd"}),),
    est_steps=1,
)

DESTRUCTIVE_SRC = "ALLOW Write-Local\nEFFECT drop"
BENIGN_SRC = "ALLOW Write-Local\nEFFECT read_file path=scratch/in.txt"


# ---------------------------------------------------------------------------
# parse_constraint
# ---------------------------------------------------------------------------

class TestParseConstraint:
    def test_parse_constraint_valid(self) -> None:
        source = (
            "# comment\n"
            "\n"
            "ALLOW Write-Local\n"
            "EFFECT read_file path=scratch/in.txt\n"
            'EFFECT write_file path="scratch/my out.txt" mode=w\n'
        )
        c = parse_constraint(source)
        assert c.ceiling == Tier.WRITE_LOCAL
        assert len(c.effects) == 2
        assert c.effects[0].op == "read_file"
        assert c.effects[0].args == {"path": "scratch/in.txt"}
        assert c.effects[1].op == "write_file"
        # quoted value with a space is kept whole.
        assert c.effects[1].args["path"] == "scratch/my out.txt"
        assert c.effects[1].args["mode"] == "w"

        # first-'=' split: path=a=b -> {"path": "a=b"}.
        c2 = parse_constraint("ALLOW Write-Local\nEFFECT foo path=a=b")
        assert c2.effects[0].args == {"path": "a=b"}

        # ALLOW with the enum name + a case-insensitive label.
        assert parse_constraint("ALLOW WRITE_LOCAL\nEFFECT read_file path=a").ceiling == Tier.WRITE_LOCAL
        assert parse_constraint("ALLOW read-only\nEFFECT read_file path=a").ceiling == Tier.READ_ONLY

    def test_parse_constraint_errors(self) -> None:
        with pytest.raises(DslParseError):  # no ALLOW
            parse_constraint("EFFECT read_file path=x")
        with pytest.raises(DslParseError):  # two ALLOW
            parse_constraint("ALLOW Read-Only\nALLOW Write-Local\nEFFECT read_file path=x")
        with pytest.raises(DslParseError):  # bad tier label
            parse_constraint("ALLOW Banana\nEFFECT read_file path=x")
        with pytest.raises(DslParseError):  # unknown directive
            parse_constraint("ALLOW Read-Only\nFOO bar\nEFFECT read_file path=x")
        with pytest.raises(DslParseError):  # EFFECT with no op
            parse_constraint("ALLOW Read-Only\nEFFECT")


# ---------------------------------------------------------------------------
# compile_constraint
# ---------------------------------------------------------------------------

class TestCompileConstraint:
    def test_compile_benign(self) -> None:
        c = parse_constraint(
            "ALLOW Write-Local\n"
            "EFFECT read_file path=scratch/in.txt\n"
            "EFFECT write_file path=scratch/out.txt"
        )
        r = compile_constraint(c)
        assert r.status is CompileStatus.COMPILED
        assert r.compiles is True
        assert r.compiled_tier is Tier.WRITE_LOCAL
        assert r.requires_human is False
        assert r.per_effect == (("read_file", Tier.READ_ONLY), ("write_file", Tier.WRITE_LOCAL))

    def test_compile_fullaccess_rejected(self) -> None:
        # "drop" (no path) -> Full-Access -> REJECTED, even under a Write-Local ceiling.
        r = compile_constraint(parse_constraint("ALLOW Write-Local\nEFFECT drop"))
        assert r.status is CompileStatus.REJECTED
        assert r.compiles is False
        assert r.requires_human is True
        assert r.compiled_tier is Tier.FULL_ACCESS

        # Full-Access NEVER self-certifies: same effect under an ALLOW Full-Access ceiling.
        r2 = compile_constraint(parse_constraint("ALLOW Full-Access\nEFFECT drop"))
        assert r2.status is CompileStatus.REJECTED
        assert r2.compiles is False
        assert r2.requires_human is True

        # delete of a protected path -> Full-Access.
        r3 = compile_constraint(parse_constraint("ALLOW Write-Local\nEFFECT delete path=/etc/passwd"))
        assert r3.status is CompileStatus.REJECTED
        assert r3.requires_human is True

    def test_compile_exceeds_ceiling(self) -> None:
        r = compile_constraint(parse_constraint("ALLOW Read-Only\nEFFECT send recipient=bob@example.com"))
        assert r.status is CompileStatus.REJECTED
        assert r.compiles is False
        assert r.compiled_tier is Tier.SOCIAL
        assert r.requires_human is False
        assert "ceiling" in r.reason.lower()

        # same effect within a Social ceiling -> COMPILED.
        r2 = compile_constraint(parse_constraint("ALLOW Social\nEFFECT send recipient=bob@example.com"))
        assert r2.status is CompileStatus.COMPILED
        assert r2.compiles is True

    def test_compile_no_declaration(self) -> None:
        r = compile_constraint(DslConstraint(ceiling=Tier.WRITE_LOCAL, effects=()))
        assert r.status is CompileStatus.NO_DECLARATION
        assert r.compiles is False


# ---------------------------------------------------------------------------
# compile_source
# ---------------------------------------------------------------------------

class TestCompileSource:
    def test_compile_source(self) -> None:
        r = compile_source("ALLOW Write-Local\nEFFECT read_file path=scratch/in.txt")
        assert r.status is CompileStatus.COMPILED
        assert r.compiles is True

        r2 = compile_source("garbage no allow here")  # no raise
        assert r2.status is CompileStatus.PARSE_ERROR
        assert r2.compiles is False

    def test_compile_args_aware_and_scope(self) -> None:
        # pay amount=0 -> Social; amount=10 -> Full-Access (G1 arg-aware).
        assert compile_source("ALLOW Social\nEFFECT pay amount=0").status is CompileStatus.COMPILED
        r2 = compile_source("ALLOW Social\nEFFECT pay amount=10")
        assert r2.status is CompileStatus.REJECTED
        assert r2.requires_human is True

        # delete of a scratch path -> Write-Local -> COMPILED without a scope.
        assert compile_source("ALLOW Write-Local\nEFFECT delete path=scratch/x").status is CompileStatus.COMPILED

        # ... but a Scope that flags scratch/x escalates it to Full-Access (scope threads into G1).
        scope = Scope(may_touch=["scratch/**"], may_not_touch=["scratch/x"])
        r4 = compile_source("ALLOW Write-Local\nEFFECT delete path=scratch/x", scope=scope)
        assert r4.status is CompileStatus.REJECTED
        assert r4.requires_human is True


# ---------------------------------------------------------------------------
# requires_dsl (no over-reach boundary)
# ---------------------------------------------------------------------------

class TestRequiresDsl:
    def test_requires_dsl_boundary(self) -> None:
        assert requires_dsl(benign_plan) is True          # opaque, benign declared surface
        assert requires_dsl(full_access_plan) is False     # already Full-Access → deterministic owns it


# ---------------------------------------------------------------------------
# make_dsl_compiler
# ---------------------------------------------------------------------------

class TestMakeDslCompiler:
    def test_make_dsl_compiler_opaque_destructive_fails(self) -> None:
        compiler = make_dsl_compiler(lambda p: DESTRUCTIVE_SRC)
        assert compiler(benign_plan) is False

    def test_make_dsl_compiler_benign_compiles(self) -> None:
        compiler = make_dsl_compiler(lambda p: BENIGN_SRC)
        assert compiler(benign_plan) is True

    def test_make_dsl_compiler_no_overreach_defers(self) -> None:
        spy = RecordingConstraintFor(DESTRUCTIVE_SRC)
        compiler = make_dsl_compiler(spy)
        # The deterministic table already classifies full_access_plan → defer (True), spy not called.
        assert compiler(full_access_plan) is True
        assert spy.calls == 0

    def test_make_dsl_compiler_no_declaration_fails(self) -> None:
        compiler = make_dsl_compiler(lambda p: None)
        assert compiler(benign_plan) is False


# ---------------------------------------------------------------------------
# G6 integration (execute_via_escape) — injected spy runner, no Docker
# ---------------------------------------------------------------------------

class TestG6Integration:
    def test_g6_integration_spy_runner(self, tmp_path: Path) -> None:
        # (a) opaque-destructive constraint -> HUMAN_INTERRUPT, runner never reached.
        spy1 = RecordingRunner()
        out1 = execute_via_escape(
            Subgoal("do it"),
            script=benign_plan,
            workspace=tmp_path,
            runner=spy1,
            dsl_compile=make_dsl_compiler(lambda p: DESTRUCTIVE_SRC),
        )
        assert out1.status is EscapeStatus.HUMAN_INTERRUPT
        assert out1.run is None
        assert spy1.calls == 0

        # (b) benign constraint -> the seam passes, classify_script benign, runner IS called.
        spy2 = RecordingRunner()
        out2 = execute_via_escape(
            Subgoal("do it"),
            script=benign_plan,
            workspace=tmp_path,
            runner=spy2,
            dsl_compile=make_dsl_compiler(lambda p: BENIGN_SRC),
        )
        assert out2.status is EscapeStatus.RAN
        assert out2.run is not None
        assert spy2.calls == 1

        # (c) no over-reach: a plan already Full-Access by the deterministic table -> the interrupt
        # fires via classify_script (not the DSL); runner never reached AND constraint_for is never
        # consulted (the orchestrator-level over-reach proof, audit r2).
        spy3 = RecordingRunner()
        cf_spy = RecordingConstraintFor(DESTRUCTIVE_SRC)
        out3 = execute_via_escape(
            Subgoal("do it"),
            script=full_access_plan,
            workspace=tmp_path,
            runner=spy3,
            dsl_compile=make_dsl_compiler(cf_spy),
        )
        assert out3.status is EscapeStatus.HUMAN_INTERRUPT
        assert out3.run is None
        assert out3.tier is Tier.FULL_ACCESS
        assert spy3.calls == 0
        assert cf_spy.calls == 0  # the DSL was not invoked — deterministic table owned the plan

        # ... identical to running WITHOUT any dsl_compile (deterministic path byte-identical).
        spy4 = RecordingRunner()
        out4 = execute_via_escape(
            Subgoal("do it"),
            script=full_access_plan,
            workspace=tmp_path,
            runner=spy4,
        )
        assert out4.status is EscapeStatus.HUMAN_INTERRUPT
        assert out4.run is None
        assert out4.tier is Tier.FULL_ACCESS
        assert spy4.calls == 0


# ---------------------------------------------------------------------------
# import purity / scope fence
# ---------------------------------------------------------------------------

class TestImportPurity:
    def test_import_purity(self) -> None:
        src_path = Path(__file__).resolve().parents[2] / "core/gui/dsl.py"
        text = src_path.read_text(encoding="utf-8")
        for banned in ("core.backends", "core.nodes", "aiohttp", "requests", "httpx", "import docker"):
            assert banned not in text, f"banned import present: {banned}"

        root = Path(__file__).resolve().parents[2]  # bobclaw-core
        result = subprocess.run(
            [sys.executable, "-c",
             "import core.gui.dsl, sys; "
             "print(any(m == 'core.backends' or m == 'core.nodes' for m in sys.modules))"],
            cwd=str(root),
            env={"PYTHONPATH": "."},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"


class TestScopeFence:
    def test_scope_fence_no_duplication(self) -> None:
        text = (Path(__file__).resolve().parents[2] / "core/gui/dsl.py").read_text(encoding="utf-8")
        # No private tier table — the DSL composes G1, never re-implements it.
        assert "_TYPE_TIER" not in text
        assert "_DANGEROUS_TOKENS" not in text
        assert "_PROTECTED_SEGMENTS" not in text
        assert "resolve_tier" in text


class TestAuditRegressions:
    """Regressions for the r1 fleet-audit fixes."""

    def test_bare_positional_arg_rejected(self) -> None:
        # audit r1: a bare/positional arg must NOT be silently dropped (which would under-classify a
        # destructive effect, e.g. `write_file /etc/passwd` -> WRITE_LOCAL instead of FULL_ACCESS).
        with pytest.raises(DslParseError):
            parse_constraint("ALLOW Write-Local\nEFFECT write_file /important/data")
        with pytest.raises(DslParseError):
            parse_constraint("ALLOW Write-Local\nEFFECT foo =bar")  # empty key
        # the whole source is now fail-closed at compile time.
        r = compile_source("ALLOW Write-Local\nEFFECT write_file /important/data")
        assert r.status is CompileStatus.PARSE_ERROR
        assert r.compiles is False

    def test_make_dsl_compiler_is_total_failclosed(self) -> None:
        # audit r1: the seam is called pre-run by execute_via_escape — it must be TOTAL + fail-closed.
        def raising_constraint_for(plan):
            raise RuntimeError("custom lookup blew up")

        compiler = make_dsl_compiler(raising_constraint_for)
        assert compiler(benign_plan) is False  # a raising callback blocks exec, never propagates

        # a non-str / non-DslConstraint declaration is also fail-closed (no AttributeError out).
        compiler2 = make_dsl_compiler(lambda p: 12345)  # bogus declaration type
        assert compiler2(benign_plan) is False

    def test_parse_crlf_source(self) -> None:
        # audit r1: a CRLF source parses identically to LF.
        c = parse_constraint("ALLOW Write-Local\r\nEFFECT read_file path=scratch/in.txt\r\n")
        assert c.ceiling == Tier.WRITE_LOCAL
        assert len(c.effects) == 1
        assert c.effects[0].op == "read_file"
        assert c.effects[0].args == {"path": "scratch/in.txt"}

    def test_unbalanced_quote_rejected(self) -> None:
        # audit r2: an unclosed quote must fail closed (else the retained leading '"' corrupts the
        # path and a destructive write under-classifies to WRITE_LOCAL).
        with pytest.raises(DslParseError):
            parse_constraint('ALLOW Write-Local\nEFFECT write_file path="/etc/passwd')
        r = compile_source('ALLOW Write-Local\nEFFECT write_file path="/etc/passwd')
        assert r.status is CompileStatus.PARSE_ERROR
        assert r.compiles is False

    def test_unknown_op_never_raises(self) -> None:
        # audit r2: compile must NEVER raise for an unknown op; it inherits G1's heuristic default
        # (an unrecognised name -> WRITE_LOCAL). It compiles under a Write-Local ceiling and is
        # REJECTED under a Read-Only ceiling — the DSL composes G1, it does not add its own default.
        r = compile_source("ALLOW Write-Local\nEFFECT unknown_op x=1")
        assert r.status is CompileStatus.COMPILED
        assert r.compiled_tier is Tier.WRITE_LOCAL
        r2 = compile_source("ALLOW Read-Only\nEFFECT unknown_op x=1")
        assert r2.status is CompileStatus.REJECTED
        assert r2.compiles is False
