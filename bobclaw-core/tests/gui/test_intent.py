"""Tests for core.gui.intent (MS2-G2 deterministic anti-desync gate)."""
from __future__ import annotations

import pathlib
import inspect
import sys

import pytest

from core.gui import A11yNode, Action, ActionKind, Frame
from core.gui.intent import (
    GateDecision,
    FormalizedIntent,
    MatchResult,
    node_key,
    hit_test,
    resolve_action_target,
    resolve_intent_target,
    action_effective_tier,
    match_intent,
    is_desync,
)
from core.gui.tiers import Tier


# ---------------------------------------------------------------------------
# 1. hit_test basic
# ---------------------------------------------------------------------------
def test_hit_test_basic():
    node_ = A11yNode(role="button", name="test", node_id="t1", bounds=(10, 10, 20, 20))
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(node_,))

    # inside
    assert hit_test(frame, (15, 15)) is node_

    # outside: left
    assert hit_test(frame, (9, 15)) is None
    # outside: x+w (exclusive)
    assert hit_test(frame, (30, 15)) is None
    # outside: below y+h (exclusive)
    assert hit_test(frame, (15, 30)) is None
    # outside: above
    assert hit_test(frame, (15, 9)) is None

    # bounds=None node never hit
    no_bounds = A11yNode(role="text", name="invisible", bounds=None)
    frame2 = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(no_bounds,))
    assert hit_test(frame2, (50, 50)) is None


# ---------------------------------------------------------------------------
# 2. hit_test nested – smallest wins
# ---------------------------------------------------------------------------
def test_hit_test_nested_smallest_wins():
    outer = A11yNode(role="div", name="outer", node_id="o", bounds=(0, 0, 100, 100))
    inner = A11yNode(role="div", name="inner", node_id="i", bounds=(40, 40, 20, 20))
    frame = Frame(seq=0, size=(200, 200), image_hash="h", a11y=(outer, inner))

    # inside both → inner (smaller area)
    assert hit_test(frame, (45, 45)) is inner
    # inside outer only
    assert hit_test(frame, (5, 5)) is outer


# ---------------------------------------------------------------------------
# 3. hit_test overlapping tie – last wins (and degenerate skipped)
# ---------------------------------------------------------------------------
def test_hit_test_overlap_tie_last_wins():
    # Both 2500 area, second overlaps first
    a = A11yNode(role="a", name="A", node_id="a", bounds=(0, 0, 50, 50))
    b = A11yNode(role="b", name="B", node_id="b", bounds=(25, 25, 50, 50))
    degenerate = A11yNode(role="d", name="d", node_id="d", bounds=(10, 10, 0, 30))  # w=0
    frame = Frame(seq=0, size=(200, 200), image_hash="h", a11y=(a, degenerate, b))

    # (30,30) is in both a and b, equal area, so last document order wins → b
    assert hit_test(frame, (30, 30)) is b
    # degenerate never hit
    assert hit_test(frame, (12, 15)) is a  # not in degenerate because w=0 skipped


# ---------------------------------------------------------------------------
# 4. node_key mirrors a11y_index
# ---------------------------------------------------------------------------
def test_node_key_mirrors_a11y_index():
    assert node_key(A11yNode(role="button", name="OK", node_id="b1")) == "b1"
    assert node_key(A11yNode(role="button", name="OK")) == "button:OK"
    assert node_key(None) == ""


# ---------------------------------------------------------------------------
# 5. faithful targeted action → ALLOW (both coord and target-only paths)
# ---------------------------------------------------------------------------
def test_faithful_targeted_action_allows():
    node_ = A11yNode(role="button", name="Save", node_id="save_btn", bounds=(0, 0, 50, 20))
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(node_,))

    # path A: coord inside
    intent = FormalizedIntent(target="save_btn", declared_tier=Tier.WRITE_LOCAL)
    action_coord = Action(ActionKind.CLICK, coord=(10, 10))
    result = match_intent(intent, action_coord, frame)
    assert result.decision is GateDecision.ALLOW
    assert result.matched is True

    # path B: no coord, target string matches
    action_target = Action(ActionKind.CLICK, target="save_btn")
    result2 = match_intent(intent, action_target, frame)
    assert result2.decision is GateDecision.ALLOW
    assert result2.matched is True


# ---------------------------------------------------------------------------
# 6. planted desync (the §5 case) → HARD_STOP
# ---------------------------------------------------------------------------
def test_planted_desync_hard_stops():
    results_list = A11yNode(role="list", name="Results", node_id="results_list", bounds=(0, 0, 500, 400))
    delete_btn = A11yNode(role="button", name="Delete account", node_id="del", bounds=(600, 10, 80, 30))
    frame = Frame(seq=0, size=(1000, 1000), image_hash="h", a11y=(results_list, delete_btn))

    # intent claims results_list, action coord lands on delete button
    intent = FormalizedIntent(target="results_list", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.CLICK, coord=(620, 20))
    result = match_intent(intent, action, frame)
    assert result.decision is GateDecision.HARD_STOP
    assert result.matched is False
    # action_tier should be FULL_ACCESS because real node is "Delete account"
    assert result.action_tier is Tier.FULL_ACCESS


# ---------------------------------------------------------------------------
# 7. coord hits no node → fail closed (HARD_STOP)
# ---------------------------------------------------------------------------
def test_coord_no_node_fail_closed():
    real_node = A11yNode(role="button", name="Exists", node_id="exists", bounds=(0, 0, 50, 50))
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(real_node,))
    intent = FormalizedIntent(target="exists", declared_tier=Tier.WRITE_LOCAL)
    action = Action(ActionKind.CLICK, coord=(9999, 9999))
    result = match_intent(intent, action, frame)
    assert result.confirmable is False
    assert result.decision is GateDecision.HARD_STOP


# ---------------------------------------------------------------------------
# 8. intent names non‑existent element → fail closed
# ---------------------------------------------------------------------------
def test_intent_ghost_element_fail_closed():
    real_node = A11yNode(role="button", name="Real", node_id="real", bounds=(0, 0, 50, 50))
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(real_node,))
    intent = FormalizedIntent(target="ghost", declared_tier=Tier.WRITE_LOCAL)
    action = Action(ActionKind.CLICK, coord=(10, 10))
    result = match_intent(intent, action, frame)
    assert result.confirmable is False
    assert result.decision is GateDecision.HARD_STOP


# ---------------------------------------------------------------------------
# 9. READ_ONLY desync → WARN
# ---------------------------------------------------------------------------
def test_readonly_desync_warns():
    # Two benign text nodes. Action = SCROLL (READ_ONLY by kind) landing on node A,
    # intent names node B (different benign name).
    node_a = A11yNode(role="text", name="A", node_id="a", bounds=(0, 0, 100, 100))
    node_b = A11yNode(role="text", name="B", node_id="b", bounds=(200, 0, 100, 100))
    frame = Frame(seq=0, size=(400, 200), image_hash="h", a11y=(node_a, node_b))

    intent = FormalizedIntent(target="b", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.SCROLL, coord=(10, 10))  # lands on node_a
    result = match_intent(intent, action, frame)
    assert result.decision is GateDecision.WARN
    assert result.decision is not GateDecision.HARD_STOP
    assert result.decision is not GateDecision.ALLOW
    assert result.action_tier is Tier.READ_ONLY


# ---------------------------------------------------------------------------
# 10. both‑targetless scroll → ALLOW
# ---------------------------------------------------------------------------
def test_both_targetless_scroll_allows():
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=())
    intent = FormalizedIntent(target="", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.SCROLL, direction="down")
    result = match_intent(intent, action, frame)
    assert result.decision is GateDecision.ALLOW
    assert result.confirmable is False


# ---------------------------------------------------------------------------
# 11. targetless FULL_ACCESS → HARD_STOP
# ---------------------------------------------------------------------------
def test_targetless_full_access_fail_closed():
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=())
    intent = FormalizedIntent(target="", declared_tier=Tier.FULL_ACCESS)
    action = Action(ActionKind.KEY, key="enter")
    # pass action_tier explicitly to force FULL_ACCESS
    result = match_intent(intent, action, frame, action_tier=Tier.FULL_ACCESS)
    assert result.decision is GateDecision.HARD_STOP
    assert result.confirmable is False


# ---------------------------------------------------------------------------
# 12. coord wins over target string
# ---------------------------------------------------------------------------
def test_coord_wins_over_target_string():
    x_node = A11yNode(role="button", name="X", node_id="x", bounds=(0, 0, 50, 50))
    y_node = A11yNode(role="button", name="Y", node_id="y", bounds=(500, 0, 50, 50))
    frame = Frame(seq=0, size=(600, 100), image_hash="h", a11y=(x_node, y_node))

    # action coord lands on X, but target claims Y; intent.target="x" → should ALLOW (coord wins)
    action = Action(ActionKind.CLICK, coord=(10, 10), target="y")
    intent_x = FormalizedIntent(target="x", declared_tier=Tier.WRITE_LOCAL)
    result_x = match_intent(intent_x, action, frame)
    assert result_x.decision is GateDecision.ALLOW
    assert result_x.matched is True

    # same action, intent.target="y" → HARD_STOP because real = X, intent = Y
    intent_y = FormalizedIntent(target="y", declared_tier=Tier.WRITE_LOCAL)
    result_y = match_intent(intent_y, action, frame)
    assert result_y.decision is GateDecision.HARD_STOP


# ---------------------------------------------------------------------------
# 13. action_effective_tier composes G1
# ---------------------------------------------------------------------------
def test_action_effective_tier_composes_g1():
    from core.gui.tiers import classify_gui_action

    # classify_gui_action base
    assert classify_gui_action(Action(ActionKind.CLICK)) is Tier.WRITE_LOCAL
    assert classify_gui_action(Action(ActionKind.SCROLL)) is Tier.READ_ONLY

    # Click with a "Delete account" real node → FULL_ACCESS
    delete_node = A11yNode(role="button", name="Delete account")
    eff = action_effective_tier(Action(ActionKind.CLICK, coord=(0, 0)), real_node=delete_node)
    assert eff is Tier.FULL_ACCESS

    # Click with "OK" node → stays WRITE_LOCAL
    ok_node = A11yNode(role="button", name="OK")
    eff2 = action_effective_tier(Action(ActionKind.CLICK), real_node=ok_node)
    assert eff2 is Tier.WRITE_LOCAL

    # Scroll with benign real node → stays READ_ONLY
    text_node = A11yNode(role="text", name="hello")
    eff3 = action_effective_tier(Action(ActionKind.SCROLL), real_node=text_node)
    assert eff3 is Tier.READ_ONLY


# ---------------------------------------------------------------------------
# 14. no-model / purity / is_desync agreement
# ---------------------------------------------------------------------------
def test_no_model_and_pure():
    # Check no forbidden imports in source file
    source_path = pathlib.Path(__file__).parents[2] / "core" / "gui" / "intent.py"
    source_text = source_path.read_text()
    forbidden = ["core.backends", "core.nodes", "_send_to_backend", "aiohttp", "requests", "httpx"]
    for token in forbidden:
        assert token not in source_text, f"Found forbidden substring in intent.py: {token!r}"

    # Build a planted desync and check is_desync agrees with hard_stop property
    results_list = A11yNode(role="list", name="Results", node_id="results_list", bounds=(0, 0, 500, 400))
    delete_btn = A11yNode(role="button", name="Delete account", node_id="del", bounds=(600, 10, 80, 30))
    frame = Frame(seq=0, size=(1000, 1000), image_hash="h", a11y=(results_list, delete_btn))

    intent = FormalizedIntent(target="results_list", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.CLICK, coord=(620, 20))
    result = match_intent(intent, action, frame)
    assert result.hard_stop is True
    assert is_desync(intent, action, frame) is True

    # faithful case
    faithful_intent = FormalizedIntent(target="del")
    faithful_action = Action(ActionKind.CLICK, coord=(630, 15))
    result_f = match_intent(faithful_intent, faithful_action, frame)
    assert result_f.hard_stop is False
    assert is_desync(faithful_intent, faithful_action, frame) is False

    # planted desync with non‑existent filesystem paths (structural only) still hard‑stops
    # (no disk path involved; already tested above)


# ---------------------------------------------------------------------------
# 15. (audit r1) hit_test is TOTAL — malformed coord / bounds never raise
# ---------------------------------------------------------------------------
def test_hit_test_total_on_malformed_inputs():
    good = A11yNode(role="button", name="OK", node_id="ok", bounds=(0, 0, 50, 50))
    # a node with a malformed bounds tuple (wrong length) and a non-numeric bounds — both must be
    # skipped, never raise (a propagating exception in a safety gate could bypass the check).
    bad_len = A11yNode(role="x", name="x", node_id="bl", bounds=(0, 0, 50))          # 3-tuple
    bad_type = A11yNode(role="y", name="y", node_id="bt", bounds=("a", "b", "c", "d"))  # non-numeric
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(bad_len, bad_type, good))
    # the good node is still found; the malformed ones are skipped without raising.
    assert hit_test(frame, (10, 10)) is good
    # malformed coords return None instead of raising (None / wrong-length / non-iterable / non-numeric)
    assert hit_test(frame, None) is None          # type: ignore[arg-type]
    assert hit_test(frame, (1,)) is None          # type: ignore[arg-type]
    assert hit_test(frame, (1, 2, 3)) is None      # type: ignore[arg-type]
    assert hit_test(frame, 5) is None             # type: ignore[arg-type]
    assert hit_test(frame, ("a", "b")) is None     # type: ignore[arg-type]
    # match_intent end-to-end never raises on a frame full of malformed bounds.
    only_bad = Frame(seq=0, size=(100, 100), image_hash="h", a11y=(bad_len, bad_type))
    res = match_intent(FormalizedIntent(target="bl"), Action(ActionKind.CLICK, coord=(10, 10)), only_bad)
    assert isinstance(res, MatchResult)  # produced a decision, did not raise


# ---------------------------------------------------------------------------
# 16. (audit r1, REJECTED-class pinned) node_key collision is benign for the THREAT MODEL
# ---------------------------------------------------------------------------
def test_node_key_collision_is_threat_model_safe():
    # Two truly-identical elements (same role+name, no node_id) collide on key by design — but they
    # share the SAME tier, so there is no danger differential to exploit. The desync THREAT (a
    # benign-declared intent masking a more-dangerous real element) requires DIFFERENT names ->
    # DIFFERENT keys -> the mismatch is ALWAYS caught. This pins that reasoning (the G4 >10%
    # bbox-overlap dedup is what refines identity beyond role:name; out of G2 scope).
    benign = A11yNode(role="button", name="View", node_id="", bounds=(0, 0, 50, 50))
    danger = A11yNode(role="button", name="Delete account", node_id="", bounds=(200, 0, 50, 50))
    frame = Frame(seq=0, size=(400, 100), image_hash="h", a11y=(benign, danger))
    # declared benign "View", real click lands on the Delete button -> different keys -> HARD_STOP.
    intent = FormalizedIntent(target="View", declared_tier=Tier.READ_ONLY)
    action = Action(ActionKind.CLICK, coord=(220, 10))  # inside the danger button
    res = match_intent(intent, action, frame)
    assert res.decision is GateDecision.HARD_STOP
    assert res.matched is False
    assert res.action_tier is Tier.FULL_ACCESS
    # two identical-tier elements DO collide (documented), but both are equally benign -> no harm.
    dup1 = A11yNode(role="button", name="OK", node_id="", bounds=(0, 0, 50, 50))
    dup2 = A11yNode(role="button", name="OK", node_id="", bounds=(200, 0, 50, 50))
    dframe = Frame(seq=0, size=(400, 100), image_hash="h", a11y=(dup1, dup2))
    # intent names "OK", click lands on the OTHER OK button: same key, same (benign) tier -> ALLOW.
    dres = match_intent(FormalizedIntent(target="OK"), Action(ActionKind.CLICK, coord=(220, 10)), dframe)
    assert dres.matched is True and dres.decision is GateDecision.ALLOW


# ---------------------------------------------------------------------------
# 17. (audit r1, REJECTED-class pinned) READ_ONLY unconfirmable WARNs, but any dangerous signal
#     ESCALATES above READ_ONLY first — so a READ_ONLY decision is provably benign.
# ---------------------------------------------------------------------------
def test_readonly_warn_is_safe_because_danger_escalates_first():
    frame = Frame(seq=0, size=(100, 100), image_hash="h",
                  a11y=(A11yNode(role="text", name="Body", node_id="body", bounds=(0, 0, 100, 100)),))
    # a genuinely benign scroll whose coord hits nothing while the intent names a real element:
    # READ_ONLY, unconfirmable -> WARN (surface, don't block — no irreversible risk). Documented.
    warn = match_intent(FormalizedIntent(target="body", declared_tier=Tier.READ_ONLY),
                        Action(ActionKind.SCROLL, coord=(9999, 9999)), frame)
    assert warn.decision is GateDecision.WARN and warn.confirmable is False
    # but a SCROLL that DECLARES a dangerous tool target is escalated above READ_ONLY by G1 and
    # therefore HARD_STOPs even though it's "just a scroll" — proving READ_ONLY stays benign-only.
    danger = match_intent(FormalizedIntent(target="body"),
                          Action(ActionKind.SCROLL, target="delete", coord=(9999, 9999)), frame)
    assert danger.action_tier is Tier.FULL_ACCESS
    assert danger.decision is GateDecision.HARD_STOP
    # and a CLICK into the void (a mutating kind) fails CLOSED regardless.
    click_void = match_intent(FormalizedIntent(target="body"),
                              Action(ActionKind.CLICK, coord=(9999, 9999)), frame)
    assert click_void.decision is GateDecision.HARD_STOP


# ---------------------------------------------------------------------------
# 18. (audit r2) a DECLARED-but-unresolved target is NOT "targetless" — it fails CLOSED.
#     The both-targetless ALLOW carve-out must require GENUINELY targetless on both sides.
# ---------------------------------------------------------------------------
def test_declared_but_unresolved_is_not_targetless():
    frame = Frame(seq=0, size=(100, 100), image_hash="h", a11y=())  # empty tree: nothing resolves
    # A CLICK that DECLARES a coord (hits nothing) + an intent naming a ghost: BOTH resolve to None,
    # but neither side is targetless (both DECLARED a target). This must HARD_STOP, NOT take the
    # both-targetless ALLOW carve-out.
    res = match_intent(FormalizedIntent(target="ghost"),
                       Action(ActionKind.CLICK, coord=(10, 10)), frame)
    assert res.decision is GateDecision.HARD_STOP
    assert res.confirmable is False
    # likewise an action declaring a target STRING that doesn't resolve + a ghost intent.
    res2 = match_intent(FormalizedIntent(target="ghost"),
                        Action(ActionKind.CLICK, target="also_ghost"), frame)
    assert res2.decision is GateDecision.HARD_STOP
    # contrast: GENUINELY targetless on both sides (no coord, no target, empty intent) -> ALLOW.
    res3 = match_intent(FormalizedIntent(target=""), Action(ActionKind.SCROLL, direction="up"), frame)
    assert res3.decision is GateDecision.ALLOW


# ---------------------------------------------------------------------------
# 19. (audit r2) the explicit action_tier override may only ESCALATE, never de-escalate the floor.
# ---------------------------------------------------------------------------
def test_action_tier_override_cannot_lower_the_floor():
    x = A11yNode(role="button", name="X", node_id="x", bounds=(0, 0, 50, 50))
    y = A11yNode(role="button", name="Y", node_id="y", bounds=(200, 0, 50, 50))
    frame = Frame(seq=0, size=(400, 100), image_hash="h", a11y=(x, y))
    intent = FormalizedIntent(target="x")
    action = Action(ActionKind.CLICK, coord=(210, 10))  # lands on Y -> a real desync (x != y)
    # a malicious/buggy caller passing READ_ONLY for a mutating CLICK desync must NOT downgrade to
    # WARN — the deterministic floor (CLICK -> WRITE_LOCAL) holds.
    res = match_intent(intent, action, frame, action_tier=Tier.READ_ONLY)
    assert res.decision is GateDecision.HARD_STOP
    assert res.action_tier is Tier.WRITE_LOCAL  # max(READ_ONLY override, WRITE_LOCAL floor)
    # an override CAN escalate above the floor (a richer schema knows it's more dangerous).
    res2 = match_intent(intent, action, frame, action_tier=Tier.FULL_ACCESS)
    assert res2.action_tier is Tier.FULL_ACCESS and res2.decision is GateDecision.HARD_STOP
