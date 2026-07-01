"""
Core/gui/grounding.py — MS2-G4 hybrid structured-first grounding fusion.

DESIGN-MS-D1 §3-G4: structured-first: a11y/DOM primary, Holo pixel/vision fallback
for canvas UIs, >10% bbox-overlap dedup so two signals on the same element collapse
to one with a11y preferred. §5: SES-tunable overlap threshold.

All functions are pure, deterministic, no I/O, no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.gui.types import Action, ActionKind, A11yNode, Frame

if TYPE_CHECKING:
    from core.gui.loop import Grounder


# ── Tunable constants (SES-knobs, DESIGN-MS-D1 §5) ──────────────────────

DEFAULT_OVERLAP_THRESHOLD: float = 0.10   # >10% bbox-overlap dedup
DEFAULT_PIXEL_HALF_EXTENT: int = 16       # half-side of synthetic click-box


# ── Geometry helpers ────────────────────────────────────────────────────

def bbox_center(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    """Return the center ``(x + w//2, y + h//2)`` of the given bounding box ``(x, y, w, h)``."""
    x, y, w, h = bbox
    return (x + w // 2, y + h // 2)


def point_to_bbox(
    coord: tuple[int, int],
    *,
    half: int = DEFAULT_PIXEL_HALF_EXTENT,
) -> tuple[int, int, int, int]:
    """Create a bounding box ``(x-half, y-half, 2*half, 2*half)`` around *coord*."""
    x, y = coord
    return (x - half, y - half, 2 * half, 2 * half)


def bbox_overlap_fraction(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> float:
    """Intersection area / min(area of *a*, area of *b*).

    Returns 0.0 if boxes are disjoint or either has area ≤ 0.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    inter_x = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    inter_y = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter_area = inter_x * inter_y
    if inter_area == 0:
        return 0.0
    area_a = aw * ah
    area_b = bw * bh
    min_area = area_a if area_a < area_b else area_b
    return inter_area / min_area


def bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over union (diagnostic only). Returns 0.0 on degenerate boxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    inter_x = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    inter_y = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter_area = inter_x * inter_y
    union_area = aw * ah + bw * bh - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


# ── Data types for the fusion result ────────────────────────────────────

@dataclass(frozen=True, slots=True)
class GroundCandidate:
    """One candidate from either the a11y or the pixel grounding path."""
    source: str                       # "a11y" | "pixel"
    action: Action
    bbox: tuple[int, int, int, int] | None
    score: float = 0.0
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class GroundDecision:
    """Complete output of the hybrid grounding process."""
    action: Action | None
    source: str                       # "" when no candidate
    candidates: tuple[GroundCandidate, ...]
    survivors: tuple[GroundCandidate, ...]
    deduped: bool


# ── Stopword set for the a11y resolver ──────────────────────────────────

_STOP: frozenset[str] = frozenset({
    "the", "a", "an", "button", "link", "input", "field", "icon",
    "dropdown", "menu", "navigation", "nav", "item", "row", "element",
    "click", "on", "to", "of", "in", "for", "please",
})


def resolve_a11y(
    subgoal: str,
    frame: Frame,
    *,
    min_score: int = 1,
) -> GroundCandidate | None:
    """Deterministic NL‑to‑node match against the accessibility tree.

    *Tokenizes* the *subgoal* into alphanumeric words, removes stopwords,
    scores each node by intersection of content tokens with name words,
    adds 0.5 for role match, then picks the best node.  Returns ``None``
    if the best score is below *min_score* or no node has bounds.
    """
    # Tokenize the subgoal fully and extract content tokens
    full_tokens = set(re.findall(r"[a-z0-9]+", subgoal.lower()))
    content_tokens = full_tokens - _STOP

    best_node: A11yNode | None = None
    best_total: float = -1.0
    best_index: int = 0
    best_area: int = 0

    for idx, node in enumerate(frame.a11y):
        if node.bounds is None:
            continue  # cannot ground to something with no bounding box
        _bx, _by, _bw, _bh = node.bounds
        if _bw <= 0 or _bh <= 0:
            continue  # a degenerate (zero-area) bbox is not a clickable target (audit r2 focus-2)
        name_words = set(re.findall(r"[a-z0-9]+", node.name.lower()))
        score = float(len(content_tokens & name_words))
        # Role bonus: if the role is mentioned verbatim in the full subgoal
        if node.role and node.role.lower() in full_tokens:
            score += 0.5

        # Tie‑breaking: higher total → smaller area → earlier index
        total = score
        x, y, w, h = node.bounds
        area = w * h
        if (total > best_total or
            (total == best_total and area < best_area) or
            (total == best_total and area == best_area and idx < best_index)):
            best_total = total
            best_node = node
            best_index = idx
            best_area = area

    if best_node is None or best_total < min_score:
        return None

    bounds = best_node.bounds
    assert bounds is not None  # already filtered
    node_id = best_node.node_id
    target = node_id if node_id else f"{best_node.role}:{best_node.name}"
    act = Action(kind=ActionKind.CLICK, target=target, coord=bbox_center(bounds))

    return GroundCandidate(
        source="a11y",
        action=act,
        bbox=bounds,
        score=best_total,
        node_id=node_id,
    )


# ── Dedup logic ─────────────────────────────────────────────────────────

def dedup_candidates(
    cands: Sequence[GroundCandidate],
    *,
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> list[GroundCandidate]:
    """Deduplicate candidates by overlapping bounding boxes.

    Priority order: ``"a11y"`` (0) > ``"pixel"`` (1) > other (2).
    Within the same source, the original order is preserved (stable).
    A candidate with ``bbox=None`` is never deduped and always kept.
    """
    # Build list with stable‑sort‑compatible keys
    priority_map = {"a11y": 0, "pixel": 1}
    indexed = [
        (priority_map.get(c.source, 2), i, c)
        for i, c in enumerate(cands)
    ]
    # Sort by (priority, original_index) — stable
    indexed.sort(key=lambda x: (x[0], x[1]))
    sorted_cands: list[GroundCandidate] = [item[2] for item in indexed]

    survivors: list[GroundCandidate] = []
    for c in sorted_cands:
        if c.bbox is None:
            survivors.append(c)
            continue
        # Check against every already‑kept higher‑priority candidate
        kept = False
        for k in survivors:
            if k.bbox is None:
                continue
            if bbox_overlap_fraction(c.bbox, k.bbox) > overlap_threshold:
                kept = True
                break
        if not kept:
            survivors.append(c)

    return survivors


# ── Hybrid grounder class ───────────────────────────────────────────────

class HybridGrounder:
    """Implements the loop's ``Grounder`` Protocol with a11y‑first fusing.

    Uses :func:`resolve_a11y` for the structured signal, optionally
    consults a pixel *grounder* (e.g. a Holo head), then deduplicates
    overlapping bounding boxes (a11y preferred).
    """

    def __init__(
        self,
        *,
        pixel_grounder: "Grounder | None" = None,
        a11y_min_score: int = 1,
        overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
        pixel_mode: str = "fallback",
        pixel_half_extent: int = DEFAULT_PIXEL_HALF_EXTENT,
    ) -> None:
        self._pixel_grounder = pixel_grounder
        self._a11y_min_score = a11y_min_score
        self._overlap_threshold = overlap_threshold
        self._pixel_mode = pixel_mode
        self._pixel_half_extent = pixel_half_extent

    def ground_detailed(self, subgoal: str, frame: Frame) -> GroundDecision:
        """Perform full grounding: a11y first, optional pixel fallback, dedup.

        Returns a :class:`GroundDecision` with all details.
        """
        # 1. Structured signal
        a11y_cand = resolve_a11y(subgoal, frame, min_score=self._a11y_min_score)

        # 2. Decide whether to get a pixel candidate
        pixel_cand: GroundCandidate | None = None
        pg = self._pixel_grounder
        if pg is not None:
            use_pixel = (
                self._pixel_mode == "always"
                or (a11y_cand is None and self._pixel_mode == "fallback")
            )
            if use_pixel:
                try:
                    act = pg.ground(subgoal, frame)
                except Exception:
                    # HybridGrounder is itself a Grounder and MUST never raise. A misbehaving injected
                    # pixel grounder (the contract says HoloGrounder never raises, but a future/3rd-party
                    # one might) is treated as "no pixel signal" rather than propagating (audit r1 focus-2).
                    act = None
                if act is not None and act.coord is not None:
                    pixel_cand = GroundCandidate(
                        source="pixel",
                        action=act,
                        bbox=point_to_bbox(act.coord, half=self._pixel_half_extent),
                        score=0.0,
                    )
                elif act is not None:
                    pixel_cand = GroundCandidate(
                        source="pixel",
                        action=act,
                        bbox=None,
                        score=0.0,
                    )

        # 3. Collect non‑None candidates
        candidates = tuple(c for c in (a11y_cand, pixel_cand) if c is not None)

        # 4. Empty case
        if not candidates:
            return GroundDecision(
                action=None,
                source="",
                candidates=(),
                survivors=(),
                deduped=False,
            )

        # 5. Dedup and choose
        survivors_list = dedup_candidates(
            candidates, overlap_threshold=self._overlap_threshold,
        )
        survivors = tuple(survivors_list)
        deduped = len(survivors) < len(candidates)
        chosen = survivors[0]

        return GroundDecision(
            action=chosen.action,
            source=chosen.source,
            candidates=candidates,
            survivors=survivors,
            deduped=deduped,
        )

    def ground(self, subgoal: str, frame: Frame) -> Action | None:
        """Return the chosen action, or ``None`` if no candidate survived."""
        return self.ground_detailed(subgoal, frame).action
