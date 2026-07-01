"""Tests for core.gui.gate + the guarded core.gui.loop hook — two-tier wiring (MS2-G3)."""

from __future__ import annotations

from pathlib import Path

from core.gui import (
    A11yNode,
    Action,
    ActionKind,
    FakeSurface,
    FailureType,
    Frame,
    GuiLoop,
    Postcondition,
    RunStatus,
    ScriptedGrounder,
    StuckConfig,
    Subgoal,
    Verdict,
    format_action,
)
from core.gui.gate import (
    GateAction,
    make_intent_gate,
    make_semantic_verifier,
    pre_act_gate,
    render_frame,
    was_human_interrupted,
)
from core.gui.intent import FormalizedIntent, GateDecision
from core.gui.loop import HUMAN_INTERRUPT_FLAG
from core.gui.tiers import Tier


# ── Reusable account frame (CONTRACTS-G3 §A) ───────────────────────────────────
RESULTS = A11yNode(role="list", name="Results", node_id="results_list", bounds=(0, 0, 500, 400))
SAVE = A11yNode(role="button", name="Save", node_id="save_btn", bounds=(20, 420, 80, 30))
DELETE = A11yNode(role="button", name="Delete account", node_id="delete_btn", bounds=(600, 10, 120, 30))
ACCT_FRAME = Frame(seq=0, size=(1000, 1000), image_hash="acct", a11y=(RESULTS, SAVE, DELETE))


# ── Tier-1 pre_act_gate ─────────────────────────────────────────────────────────


def test_pre_act_gate_faithful_proceeds():
    """Faithful non-Full-Access action -> PROCEED."""
    intent = FormalizedIntent(target="save_btn", declared_tier=Tier.WRITE_LOCAL)
    action = Action(ActionKind.CLICK, coord=(40, 430))  # inside save_btn
    outcome = pre_act_gate(intent, action, ACCT_FRAME)
    assert outcome.action is GateAction.PROCEED
    assert outcome.actuates is True
    assert outcome.tier is Tier.WRITE_LOCAL


def test_pre_act_gate_planted_desync_blocks():
    """The §5 planted desync (declared benign list, real click on Delete) -> BLOCK + FULL_ACCESS."""
    intent = FormalizedIntent(target="results_list", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.CLICK, coord=(620, 20))  # inside delete_btn
    outcome = pre_act_gate(intent, action, ACCT_FRAME)
    assert outcome.action is GateAction.BLOCK
    assert outcome.blocked is True
    assert outcome.tier is Tier.FULL_ACCESS
    assert outcome.match.hard_stop is True


def test_pre_act_gate_faithful_full_access_blocks_via_g1():
    """A FAITHFUL Full-Access action: G2 says ALLOW (no desync) but G1 BLOCKs on the tier."""
    intent = FormalizedIntent(target="delete_btn", declared_tier=Tier.FULL_ACCESS)
    action = Action(ActionKind.CLICK, coord=(620, 20))  # inside the HONESTLY-named delete_btn
    outcome = pre_act_gate(intent, action, ACCT_FRAME)
    assert outcome.match.decision is GateDecision.ALLOW  # G2 sees a faithful action
    assert outcome.action is GateAction.BLOCK            # G1 still interrupts Full-Access
    assert outcome.tier is Tier.FULL_ACCESS


def test_pre_act_gate_readonly_desync_warns():
    """A READ_ONLY desync -> WARN (surface, still actuate)."""
    pa = A11yNode(role="text", name="A", node_id="pa", bounds=(0, 0, 100, 100))
    pb = A11yNode(role="text", name="B", node_id="pb", bounds=(200, 0, 100, 100))
    frame = Frame(seq=0, size=(300, 200), image_hash="ab", a11y=(pa, pb))
    intent = FormalizedIntent(target="pb", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.SCROLL, coord=(10, 10))  # lands on pa, intent named pb -> desync
    outcome = pre_act_gate(intent, action, frame)
    assert outcome.action is GateAction.WARN
    assert outcome.actuates is True
    assert outcome.tier is Tier.READ_ONLY


def test_pre_act_gate_void_fail_closed():
    """Coord hits nothing on a mutating tier -> BLOCK (fail closed), unconfirmable."""
    intent = FormalizedIntent(target="save_btn", declared_tier=Tier.WRITE_LOCAL)
    action = Action(ActionKind.CLICK, coord=(9999, 9999))
    outcome = pre_act_gate(intent, action, ACCT_FRAME)
    assert outcome.action is GateAction.BLOCK
    assert outcome.match.confirmable is False


def test_make_intent_gate_uses_map_and_default():
    """make_intent_gate uses the declared-intent map; derives a faithful default when absent."""
    gate = make_intent_gate({"do x": FormalizedIntent(target="results_list", declared_tier=Tier.READ_ONLY)})

    # Mapped subgoal: declared benign list, real click on the delete button -> BLOCK (desync).
    outcome = gate(Subgoal("do x"), Action(ActionKind.CLICK, coord=(620, 20)), ACCT_FRAME)
    assert outcome.action is GateAction.BLOCK

    # Unmapped subgoal with a faithful target (derived default intent target=save_btn) -> PROCEED.
    outcome = gate(Subgoal("save it"), Action(ActionKind.CLICK, target="save_btn"), ACCT_FRAME)
    assert outcome.action is GateAction.PROCEED


# ── Tier-2 semantic verifier (mocked send; no real backend) ─────────────────────


def test_make_semantic_verifier_holds_mocked():
    """A 'holds' critic verdict -> Verdict ok=True with a tier2-semantic criterion."""
    async def fake_send(messages, backend):
        return '{"verdict":"holds","reasons":["ok"]}'

    verifier = make_semantic_verifier(actor_backend="glm_5_2", send=fake_send)
    verdict = verifier(
        Subgoal("the file is saved", Postcondition(expect_changed=False)),
        Action(ActionKind.CLICK, target="save_btn"),
        ACCT_FRAME,
        ACCT_FRAME,
        None,
    )
    # MS-2's make_postcondition_verifier is a SYNC Callable[[dict], bool] (it bridges the async critic
    # via _run_blocking), so verdict.ok is a real bool — NOT a truthy coroutine. The 'holds' vs
    # 'violated' discrimination below (True here, False in the next test, same construction) proves it:
    # a truthy-coroutine bug would make BOTH return True and break the next test.
    assert verdict.ok is True
    assert isinstance(verdict.ok, bool)
    assert ("tier2-semantic", True) in verdict.criteria


def test_make_semantic_verifier_failsafe_mocked():
    """'violated' and unparseable critic replies both -> ok=False (fail-safe).

    This is the decisive pin against the "synchronous call of an async critic returns a truthy
    coroutine -> auto-pass" theory: a coroutine would be truthy and these would assert ok=True and
    FAIL. They pass with ok=False, proving _bool(payload) returns a real, discriminating bool.
    """
    async def fake_send_violated(messages, backend):
        return '{"verdict":"violated"}'

    async def fake_send_garbage(messages, backend):
        return "not json at all"

    v1 = make_semantic_verifier(actor_backend="glm_5_2", send=fake_send_violated)
    verdict1 = v1(Subgoal("x", Postcondition(expect_changed=False)),
                  Action(ActionKind.CLICK, target="save_btn"), ACCT_FRAME, ACCT_FRAME, None)
    assert verdict1.ok is False

    v2 = make_semantic_verifier(actor_backend="glm_5_2", send=fake_send_garbage)
    verdict2 = v2(Subgoal("x", Postcondition(expect_changed=False)),
                  Action(ActionKind.CLICK, target="save_btn"), ACCT_FRAME, ACCT_FRAME, None)
    assert verdict2.ok is False


def test_render_frame_deterministic():
    """render_frame is deterministic and total (header line even for an empty a11y tree)."""
    r1 = render_frame(ACCT_FRAME)
    r2 = render_frame(ACCT_FRAME)
    assert r1 == r2
    assert "Delete account" in r1
    assert "image_hash=acct" in r1

    empty = Frame(seq=0, size=(1, 1), image_hash="x", a11y=())
    rendered = render_frame(empty)
    assert "size=(1, 1)" in rendered
    assert "image_hash=x" in rendered


# ── Loop integration: the guarded GuiLoop hook ──────────────────────────────────


def _surface_for_danger() -> FakeSurface:
    """A FakeSurface that WOULD transition on the delete click — so a non-actuation is provable."""
    changed = Frame(seq=1, size=(1000, 1000), image_hash="changed", a11y=())
    return FakeSurface(
        states={"start": ACCT_FRAME, "changed": changed},
        transitions={
            ("start", format_action(Action(ActionKind.CLICK, coord=(620, 20), target="delete_btn"))): "changed",
            ("start", format_action(Action(ActionKind.CLICK, coord=(620, 20)))): "changed",
        },
        start="start",
    )


def test_loop_full_access_interrupt_pre_actuation():
    """A faithful Full-Access action is interrupted BEFORE surface.act (no actuation, no state change)."""
    surface = _surface_for_danger()
    grounder = ScriptedGrounder({"danger": Action(ActionKind.CLICK, coord=(620, 20), target="delete_btn")})
    gate = make_intent_gate({"danger": FormalizedIntent(target="delete_btn", declared_tier=Tier.FULL_ACCESS)})
    result = GuiLoop(surface, grounder, gate=gate).run([Subgoal("danger", Postcondition())])

    assert result.status is RunStatus.STUCK
    last = result.steps[-1]
    assert last.flag == HUMAN_INTERRUPT_FLAG
    assert last.failure is FailureType.AUTH_BLOCK
    assert last.diff is None            # never actuated -> no diff
    assert last.action is not None      # the proposed (blocked) action is surfaced
    assert result.completed == 0
    assert was_human_interrupted(result) is True
    # The surface never advanced (a transition existed for the action, yet state == start).
    assert surface.capture().image_hash == "acct"


def test_loop_desync_hard_stopped_in_loop():
    """A desync (G2) is hard-stopped INSIDE the running loop, before actuation."""
    surface = _surface_for_danger()
    grounder = ScriptedGrounder({"scroll list": Action(ActionKind.CLICK, coord=(620, 20))})
    gate = make_intent_gate({"scroll list": FormalizedIntent(target="results_list", declared_tier=Tier.READ_ONLY)})
    result = GuiLoop(surface, grounder, gate=gate).run([Subgoal("scroll list", Postcondition())])

    assert result.status is RunStatus.STUCK
    last = result.steps[-1]
    assert last.flag == HUMAN_INTERRUPT_FLAG
    assert last.failure is FailureType.AUTH_BLOCK
    assert last.diff is None
    assert result.completed == 0
    assert was_human_interrupted(result) is True
    assert surface.capture().image_hash == "acct"  # G2 fired in-loop -> no actuation


def test_loop_no_seam_byte_identical():
    """No-seam run (gate=None, semantic_verifier=None) reproduces the skeleton result exactly."""
    home = Frame(seq=0, size=(100, 100), image_hash="home",
                 a11y=(A11yNode(role="button", name="menu", node_id="menu"),))
    menu = Frame(seq=0, size=(100, 100), image_hash="menu",
                 a11y=(A11yNode(role="button", name="settings", node_id="settings"),))
    settings = Frame(seq=0, size=(100, 100), image_hash="settings",
                     a11y=(A11yNode(role="group", name="panel", node_id="panel"),))
    click_menu = Action(ActionKind.CLICK, target="menu")
    click_settings = Action(ActionKind.CLICK, target="settings")
    surface = FakeSurface(
        states={"home": home, "menu": menu, "settings": settings},
        transitions={
            ("home", format_action(click_menu)): "menu",
            ("menu", format_action(click_settings)): "settings",
        },
        start="home",
    )
    grounder = ScriptedGrounder({"open menu": click_menu, "open settings": "click(target=settings)"})
    plan = [
        Subgoal("open menu", Postcondition(present=("settings",))),
        Subgoal("open settings", Postcondition(present=("panel",))),
    ]
    result = GuiLoop(surface, grounder).run(plan)
    assert result.status is RunStatus.COMPLETED
    assert result.completed == 2
    assert result.total == 2
    verified = [s for s in result.steps if s.verdict and s.verdict.ok]
    assert len(verified) == 2
    assert all(s.failure is FailureType.NONE for s in verified)


def test_loop_tier2_escalates_only_when_floor_cannot_judge():
    """Tier-2 escalation fires ONLY for a subgoal whose structural floor has no judgeable criteria."""

    class StubVerifier:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, sg, action, pre, post, diff) -> Verdict:
            self.calls.append(sg.text)
            return Verdict(ok=True, reason="stub holds", criteria=(("tier2-semantic", True),))

    target = A11yNode(role="button", name="target", node_id="target", bounds=(0, 0, 100, 50))
    panel = A11yNode(role="group", name="panel", node_id="panel", bounds=(0, 0, 50, 30))
    s0 = Frame(seq=0, size=(200, 200), image_hash="s0", a11y=(target,))
    s1 = Frame(seq=0, size=(200, 200), image_hash="s1", a11y=(target,))   # after the 1st click
    s2 = Frame(seq=0, size=(200, 200), image_hash="s2", a11y=(panel,))    # has the structural node
    click_t = Action(ActionKind.CLICK, target="target")
    surface = FakeSurface(
        states={"s0": s0, "s1": s1, "s2": s2},
        transitions={
            ("s0", format_action(click_t)): "s1",
            ("s1", format_action(click_t)): "s2",
        },
        start="s0",
    )
    grounder = ScriptedGrounder({"no criteria": click_t, "structural": click_t})
    stub = StubVerifier()
    plan = [
        Subgoal("no criteria", Postcondition(expect_changed=False)),   # no structural criteria -> escalate
        Subgoal("structural", Postcondition(present=("panel",))),      # structural floor judges -> no escalate
    ]
    result = GuiLoop(surface, grounder, semantic_verifier=stub).run(plan)

    assert result.status is RunStatus.COMPLETED
    assert result.completed == 2
    # Only the no-criteria subgoal escalated to the critic.
    assert stub.calls == ["no criteria"]
    step = next(s for s in result.steps if s.subgoal == "no criteria")
    assert step.verdict is not None
    assert ("tier2-semantic", True) in step.verdict.criteria


def test_loop_tier2_veto_marks_audit_veto():
    """A Tier-2 veto (ok=False) on an actuated action marks the step FailureType.AUDIT_VETO."""

    def veto(sg, action, pre, post, diff) -> Verdict:
        return Verdict(ok=False, reason="stub violated", criteria=(("tier2-semantic", False),))

    target = A11yNode(role="button", name="target", node_id="target", bounds=(0, 0, 100, 50))
    pre = Frame(seq=0, size=(200, 200), image_hash="pre", a11y=(target,))
    post = Frame(seq=0, size=(200, 200), image_hash="post", a11y=())
    click_t = Action(ActionKind.CLICK, target="target")
    surface = FakeSurface(
        states={"pre": pre, "post": post},
        transitions={("pre", format_action(click_t)): "post"},
        start="pre",
    )
    grounder = ScriptedGrounder({"actuate": click_t})
    # Cap to a single step so steps[-1] is the actuated (state-changing) step, not a later no-op retry.
    cfg = StuckConfig(max_steps=1)
    result = GuiLoop(surface, grounder, cfg=cfg, semantic_verifier=veto).run(
        [Subgoal("actuate", Postcondition(expect_changed=False))]
    )

    step = result.steps[-1]
    assert step.failure is FailureType.AUDIT_VETO      # the reserved cross-family-critic veto
    assert step.diff is not None and step.diff.changed is True  # the action DID actuate
    assert step.verdict is not None and step.verdict.ok is False


def test_loop_gate_exception_fails_closed():
    """A misbehaving gate (raises) FAILS CLOSED in-loop: human interrupt, no actuation (audit r2)."""
    def boom_gate(subgoal, action, frame):
        raise RuntimeError("gate blew up")

    surface = _surface_for_danger()
    grounder = ScriptedGrounder({"danger": Action(ActionKind.CLICK, coord=(620, 20), target="delete_btn")})
    result = GuiLoop(surface, grounder, gate=boom_gate).run([Subgoal("danger", Postcondition())])

    assert result.status is RunStatus.STUCK
    last = result.steps[-1]
    assert last.flag == HUMAN_INTERRUPT_FLAG
    assert last.failure is FailureType.AUTH_BLOCK
    assert was_human_interrupted(result) is True
    assert surface.capture().image_hash == "acct"  # never actuated despite the gate crash


def test_loop_semantic_verifier_exception_fails_safe():
    """A misbehaving semantic_verifier (raises) FAILS SAFE in-loop: not-ok + AUDIT_VETO (audit r2)."""
    def boom_verifier(sg, action, pre, post, diff):
        raise RuntimeError("verifier blew up")

    target = A11yNode(role="button", name="target", node_id="target", bounds=(0, 0, 100, 50))
    pre = Frame(seq=0, size=(200, 200), image_hash="pre", a11y=(target,))
    post = Frame(seq=0, size=(200, 200), image_hash="post", a11y=())
    click_t = Action(ActionKind.CLICK, target="target")
    surface = FakeSurface(
        states={"pre": pre, "post": post},
        transitions={("pre", format_action(click_t)): "post"},
        start="pre",
    )
    grounder = ScriptedGrounder({"actuate": click_t})
    cfg = StuckConfig(max_steps=1)
    result = GuiLoop(surface, grounder, cfg=cfg, semantic_verifier=boom_verifier).run(
        [Subgoal("actuate", Postcondition(expect_changed=False))]
    )
    step = result.steps[-1]
    assert step.failure is FailureType.AUDIT_VETO       # never auto-passes on a verifier crash
    assert step.verdict is not None and step.verdict.ok is False


def test_gate_import_purity():
    """gate.py is import-light: no backend/model/HTTP strings in source AND a fresh-interpreter
    import pulls in NO backend/node/HTTP module (the MS-2 + loop imports are lazy, inside functions)."""
    import subprocess
    import sys

    src = (Path(__file__).parents[2] / "core" / "gui" / "gate.py").read_text(encoding="utf-8")
    for token in ("core.backends", "core.nodes", "aiohttp", "requests", "httpx"):
        assert token not in src, f"gate.py contains forbidden string: {token}"

    # Real import-graph probe: importing core.gui.gate must drag in no backend/node/HTTP module.
    probe = (
        "import sys; import core.gui.gate; "
        "bad=[m for m in sys.modules if m.startswith('core.backends') or m.startswith('core.nodes') "
        "or m in ('aiohttp','requests','httpx')]; "
        "assert not bad, bad; print('clean')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).parents[2]),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"import-graph probe failed: {proc.stdout!r} {proc.stderr!r}"
    assert proc.stdout.strip() == "clean"
