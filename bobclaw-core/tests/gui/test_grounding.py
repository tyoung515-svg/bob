from __future__ import annotations

from core.gui.types import A11yNode, Action, ActionKind, Frame
from core.gui.grounding import (
    DEFAULT_OVERLAP_THRESHOLD,
    bbox_center,
    point_to_bbox,
    bbox_overlap_fraction,
    bbox_iou,
    GroundCandidate,
    GroundDecision,
    resolve_a11y,
    dedup_candidates,
    HybridGrounder,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeGrounder:
    """Fake Grounder that returns a fixed action and counts calls (no model)."""

    def __init__(self, action: Action):
        self.action = action
        self.calls = 0

    def ground(self, subgoal: str, frame: Frame) -> Action | None:
        self.calls += 1
        return self.action


def nd(role: str, name: str, x: int, y: int, w: int, h: int, node_id: str = "") -> A11yNode:
    """Construct an A11yNode WITH bounds directly (conftest's node() has no bounds)."""
    return A11yNode(role=role, name=name, node_id=node_id, bounds=(x, y, w, h))


def fr(*nodes: A11yNode) -> Frame:
    return Frame(seq=1, size=(1280, 1024), image_hash="h", a11y=tuple(nodes))


# ---------------------------------------------------------------------------
# 1. geometry
# ---------------------------------------------------------------------------

def test_bbox_geometry() -> None:
    assert bbox_center((10, 20, 30, 40)) == (25, 40)
    assert point_to_bbox((100, 100), half=16) == (84, 84, 32, 32)
    # identical boxes -> full overlap
    assert bbox_overlap_fraction((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    # small box fully inside big box -> 1.0 (containment-aware: overlap / min-area)
    assert bbox_overlap_fraction((2, 2, 2, 2), (0, 0, 100, 100)) == 1.0
    # disjoint -> 0.0
    assert bbox_overlap_fraction((0, 0, 5, 5), (100, 100, 5, 5)) == 0.0
    # degenerate box (w=0) -> 0.0
    assert bbox_overlap_fraction((0, 0, 0, 10), (0, 0, 10, 10)) == 0.0
    # iou
    assert bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert 0.0 < bbox_iou((0, 0, 10, 10), (5, 0, 10, 10)) < 1.0


# ---------------------------------------------------------------------------
# 2-4. a11y resolution (the structured signal)
# ---------------------------------------------------------------------------

def test_resolve_a11y_matches_named_node() -> None:
    f = fr(
        nd("button", "Save changes", 10, 10, 100, 30, "save"),
        nd("button", "Cancel", 120, 10, 80, 30, "cancel"),
        nd("button", "Delete account", 210, 10, 120, 30, "del"),
    )
    cand = resolve_a11y("the Save changes button", f)
    assert cand is not None
    assert cand.node_id == "save"
    assert cand.source == "a11y"
    assert cand.action.kind == ActionKind.CLICK
    assert cand.action.coord == bbox_center((10, 10, 100, 30))  # (60, 25)

    cand2 = resolve_a11y("the Cancel button", f)
    assert cand2 is not None
    assert cand2.node_id == "cancel"


def test_resolve_a11y_prefers_specific_on_tie() -> None:
    # the link inside the row is smaller (and gets the role bonus) -> wins the tie
    f = fr(
        nd("listitem", "Row two More info", 10, 10, 400, 40, "row2"),
        nd("link", "More info", 30, 18, 60, 20, "link2"),
    )
    cand = resolve_a11y("the More info link", f)
    assert cand is not None
    assert cand.node_id == "link2"


def test_resolve_a11y_none_when_no_match() -> None:
    f = fr(nd("button", "Save", 10, 10, 50, 20, "s"))
    assert resolve_a11y("the rocket launch sequence", f) is None
    # a node with no bounds is skipped (cannot click / dedup)
    f2 = fr(A11yNode(role="button", name="Save", node_id="s", bounds=None))
    assert resolve_a11y("the Save button", f2) is None


def test_resolve_a11y_skips_degenerate_bbox() -> None:
    # audit r2 focus-2: a zero-area a11y node is not a clickable target — skip it (don't return a
    # degenerate candidate). The real, positive-area node must win even with a lower-or-equal score.
    f = fr(
        A11yNode(role="button", name="Save", node_id="ghost", bounds=(10, 10, 0, 0)),
        nd("button", "Save", 200, 10, 80, 30, "real"),
    )
    cand = resolve_a11y("the Save button", f)
    assert cand is not None
    assert cand.node_id == "real"
    assert cand.bbox == (200, 10, 80, 30)
    # ONLY a degenerate match available -> None (no degenerate candidate ever returned)
    f2 = fr(A11yNode(role="button", name="Save", node_id="ghost", bounds=(10, 10, 0, 5)))
    assert resolve_a11y("the Save button", f2) is None


# ---------------------------------------------------------------------------
# 5-7. dedup (the >10% bbox-overlap fusion)
# ---------------------------------------------------------------------------

def test_dedup_overlapping_a11y_wins() -> None:
    a11y = GroundCandidate(
        source="a11y",
        action=Action(kind=ActionKind.CLICK, coord=(50, 25)),
        bbox=(10, 10, 100, 30),
        score=2.0,
        node_id="save",
    )
    pix = GroundCandidate(
        source="pixel",
        action=Action(kind=ActionKind.CLICK, coord=(55, 25)),
        bbox=point_to_bbox((55, 25)),
        score=0.0,
    )
    # input order should not matter — a11y is preferred by priority
    out = dedup_candidates([pix, a11y])
    assert len(out) == 1
    assert out[0].source == "a11y"


def test_dedup_non_overlapping_keeps_both() -> None:
    a11y = GroundCandidate(
        source="a11y",
        action=Action(kind=ActionKind.CLICK, coord=(10, 10)),
        bbox=(0, 0, 20, 20),
        score=2.0,
        node_id="save",
    )
    pix = GroundCandidate(
        source="pixel",
        action=Action(kind=ActionKind.CLICK, coord=(500, 500)),
        bbox=point_to_bbox((500, 500)),
        score=0.0,
    )
    out = dedup_candidates([a11y, pix])
    assert len(out) == 2
    assert out[0].source == "a11y"
    assert out[1].source == "pixel"


def test_dedup_threshold_boundary() -> None:
    # box_a area = 100 (the min area). box_b overlaps a 10x1 strip = area 10 -> fraction 0.10.
    # 0.10 is NOT > 0.10 (threshold is strictly >) -> both kept.
    box_a = (0, 0, 10, 10)
    box_b = (0, 9, 10, 10)  # y-overlap [9,10] = 1px tall, 10px wide -> inter area 10 -> 0.10
    cand_a = GroundCandidate("a11y", Action(kind=ActionKind.CLICK), box_a)
    cand_b = GroundCandidate("pixel", Action(kind=ActionKind.CLICK), box_b)
    assert bbox_overlap_fraction(box_a, box_b) == 0.10
    out = dedup_candidates([cand_a, cand_b], overlap_threshold=0.10)
    assert len(out) == 2  # exactly-at-threshold is kept

    # shift up 1px -> 10x2 strip = area 20 -> fraction 0.20 > 0.10 -> deduped
    box_b2 = (0, 8, 10, 10)
    cand_b2 = GroundCandidate("pixel", Action(kind=ActionKind.CLICK), box_b2)
    assert bbox_overlap_fraction(box_a, box_b2) == 0.20
    out2 = dedup_candidates([cand_a, cand_b2], overlap_threshold=0.10)
    assert len(out2) == 1
    assert out2[0].source == "a11y"


# ---------------------------------------------------------------------------
# 8-11. HybridGrounder (structured-first orchestration)
# ---------------------------------------------------------------------------

def test_hybrid_a11y_first_no_pixel_call() -> None:
    fake = FakeGrounder(Action(kind=ActionKind.CLICK, coord=(999, 999)))
    h = HybridGrounder(pixel_grounder=fake, pixel_mode="fallback")
    f = fr(nd("button", "Save changes", 10, 10, 100, 30, "save"))
    dec = h.ground_detailed("the Save changes button", f)
    assert dec.source == "a11y"
    assert dec.action.coord == bbox_center((10, 10, 100, 30))  # (60, 25)
    assert fake.calls == 0  # a11y present -> pixel NOT called in fallback mode
    assert h.ground("the Save changes button", f) == dec.action


def test_hybrid_pixel_fallback_when_no_a11y() -> None:
    fake = FakeGrounder(Action(kind=ActionKind.CLICK, coord=(300, 300)))
    h = HybridGrounder(pixel_grounder=fake, pixel_mode="fallback")
    # a <canvas> node has no name -> the drawn target has no a11y node -> a11y absent
    f = fr(nd("canvas", "", 10, 10, 400, 400, "cv"))
    dec = h.ground_detailed("the round play button drawn on the canvas", f)
    assert dec.source == "pixel"
    assert dec.action.coord == (300, 300)
    assert fake.calls == 1


def test_hybrid_always_mode_dedups() -> None:
    fake = FakeGrounder(Action(kind=ActionKind.CLICK, coord=(55, 25)))
    h = HybridGrounder(pixel_grounder=fake, pixel_mode="always")
    f = fr(nd("button", "Save changes", 10, 10, 100, 30, "save"))
    dec = h.ground_detailed("the Save changes button", f)
    assert dec.deduped is True
    assert dec.source == "a11y"
    assert len(dec.candidates) == 2   # a11y + pixel
    assert len(dec.survivors) == 1    # pixel deduped away
    assert fake.calls == 1            # pixel WAS called in always mode


def test_hybrid_none_when_neither() -> None:
    h = HybridGrounder(pixel_grounder=None, pixel_mode="fallback")
    f = fr(nd("button", "Save", 10, 10, 40, 20, "s"))
    dec = h.ground_detailed("the rocket launch", f)
    assert dec.action is None
    assert dec.source == ""
    assert dec.candidates == ()
    assert dec.deduped is False
    assert h.ground("the rocket launch", f) is None


class _RaisingGrounder:
    """A pixel grounder that always raises — the HybridGrounder must absorb it (never raise)."""

    def ground(self, subgoal: str, frame: Frame) -> Action | None:
        raise RuntimeError("pixel head exploded")


def test_hybrid_absorbs_raising_pixel_grounder() -> None:
    # audit r1 focus-2: HybridGrounder is itself a Grounder and MUST never raise even if the injected
    # pixel grounder raises. Fallback (a11y absent) -> no pixel signal -> action None (no crash).
    h = HybridGrounder(pixel_grounder=_RaisingGrounder(), pixel_mode="fallback")
    f = fr(nd("canvas", "", 10, 10, 400, 400, "cv"))
    dec = h.ground_detailed("the play button on the canvas", f)
    assert dec.action is None and dec.source == ""
    assert h.ground("the play button on the canvas", f) is None

    # 'always' mode with a present a11y signal: the raising pixel grounder is absorbed, a11y still wins.
    h2 = HybridGrounder(pixel_grounder=_RaisingGrounder(), pixel_mode="always")
    f2 = fr(nd("button", "Save changes", 10, 10, 100, 30, "save"))
    dec2 = h2.ground_detailed("the Save changes button", f2)
    assert dec2.source == "a11y"
    assert dec2.action.coord == bbox_center((10, 10, 100, 30))


def test_default_overlap_threshold_is_ten_percent() -> None:
    assert DEFAULT_OVERLAP_THRESHOLD == 0.10
