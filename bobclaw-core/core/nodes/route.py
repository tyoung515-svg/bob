"""
BoBClaw Core — Model routing node

Reads face preferences + user override, probes local backends,
and writes the chosen backend name into state.backend.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from core import teams
from core.backends.local_router import LocalModelRouter

if TYPE_CHECKING:
    from core.graph import AgentState

logger = logging.getLogger(__name__)

# Module-level router — replace in tests via monkeypatch
_router = LocalModelRouter()

# ── Face-swap heuristics ─────────────────────────────────────────────────────

_PLAN_INTENT = re.compile(
    r"\b(plan|design|architect|spec|roadmap|decompose|break\s+down|figure\s+out\s+how|propose|outline|map\s+out|scope)\b",
    re.IGNORECASE,
)
# Bulk-dispatch threshold: when the planner produces this many subtasks or more,
# upgrade worker-kimi → worker-kimi-bulk so the swarm runs on PAYG instead of
# the membership-tier rate-limited endpoint.
_BULK_DISPATCH_THRESHOLD = 5

_CODE_SHAPED = re.compile(
    r"\b(refactor|migrate|implement|integrate|repo|module|class|function|endpoint|api|test|build|compile|deploy|pipeline|service|backend|frontend|schema|sql|crud|fix|patch|bug|merge|pr|commit|library|package|dependency)\b",
    re.IGNORECASE,
)

# CoCouncil (P1b): the face that triggers the multi-voice deliberation branch.
_COUNCIL_FACE_ID = "council-max"


def _build_council_spec(state: "AgentState") -> dict:
    """Build the ``council_spec`` for the ``council-max`` face.

    Carries ``{mode, seats, synth_backend}``. The mode comes from
    ``COUNCIL_MODE_DEFAULT`` unless the request's ``model_override`` names a
    shape ("fusion" / "sequential") — a lightweight per-request shape hint that
    doesn't disturb the normal model_override backend path (council-max routes
    to its own subgraph, not to a backend). Absent/unknown override → default.
    Threaded onto every council-face return path the same way ``cc_posture`` is.
    """
    from core.config import (
        COUNCIL_DEFAULT_SEATS,
        COUNCIL_DEFAULT_SYNTH_POSTURE,
        COUNCIL_MODE_DEFAULT,
        COUNCIL_SEAT_BACKENDS,
    )

    override = (state.get("model_override") or "").strip().lower()
    mode = override if override in ("fusion", "sequential") else COUNCIL_MODE_DEFAULT
    synth_backend = COUNCIL_SEAT_BACKENDS[COUNCIL_DEFAULT_SYNTH_POSTURE]["backend"]
    spec: dict = {
        "mode": mode,
        "seats": list(COUNCIL_DEFAULT_SEATS),
        "synth_backend": synth_backend,
    }
    # MS9-W1 (live theater opt-in): stamp the U7 emit gate ONLY when the request opted in
    # (state["emit_events"] truthy). Absent/falsy ⇒ key NOT added ⇒ spec byte-identical ⇒
    # events_enabled() False ⇒ nothing new emitted. The gate is the load-bearing safety.
    if state.get("emit_events"):
        spec["emit_events"] = True
    return spec


def _build_council_spec_from_profile(profile: dict, *, emit_events: bool = False) -> dict:
    """Compile a council-shaped profile into a ``council_spec`` for the panel/council
    nodes. Generalizes :func:`_build_council_spec` to any saved profile: its seats
    (postures) + per-seat backend/role_prompt overrides (the ``panel.py`` ``profile``
    hook) + ``synth_backend`` + ``bounds`` (consumed in Phase 3b). A seat that omits a
    backend inherits the posture default; a seat that sets only a role_prompt steers
    the angle without changing the vendor.

    MS9-W1: ``emit_events`` stamps the U7 live-theater gate onto the spec (default False ⇒
    key NOT added ⇒ byte-identical). Mirrors :func:`_build_council_spec`.
    """
    from core.config import (
        COUNCIL_DEFAULT_SEATS,
        COUNCIL_DEFAULT_SYNTH_POSTURE,
        COUNCIL_SEAT_BACKENDS,
    )

    seats_cfg = profile.get("seats")
    if not seats_cfg:
        # Derive council seats from the roster: each role's PRIMARY slot becomes a
        # seat (posture = role; backend + role_prompt from the slot). The roster
        # doubles as the council seats so ONE builder authors both JOAT + council.
        seats_cfg = []
        for role, slots in (profile.get("roles") or {}).items():
            if slots:
                primary = slots[0]
                seats_cfg.append({
                    "posture": role,
                    "backend": primary.get("backend"),
                    "fallback_chain": list(primary.get("escalation_chain") or []),
                    "role_prompt": primary.get("role_prompt", ""),
                })
    postures: list[str] = []
    seat_profile: dict = {}
    for s in seats_cfg:
        posture = s.get("posture")
        if not posture:
            continue
        postures.append(posture)
        entry: dict = {"role_prompt": s.get("role_prompt", "")}
        if s.get("backend"):
            entry["backend"] = s["backend"]
            entry["fallback_chain"] = list(s.get("fallback_chain") or [])
        seat_profile[posture] = entry
    default_synth = COUNCIL_SEAT_BACKENDS[COUNCIL_DEFAULT_SYNTH_POSTURE]["backend"]
    mode = profile.get("shape") or "fusion"
    bounds = profile.get("protocol_bounds") or {}
    # A debate run closes at debate_converge (never the grounding gate), so a
    # grounding bound is inert for it — warn once at compile time so an author who
    # set grounding:on on a debate profile isn't silently surprised.
    if mode == "debate" and bounds.get("grounding") is not None:
        from core.nodes.grounding import grounding_enabled
        if grounding_enabled({"bounds": bounds}):
            logger.warning("profile %r: shape=debate uses the debate convergence "
                           "gate, so the grounding bound is IGNORED (web grounding "
                           "for debate is not wired yet)", profile.get("name"))
    spec: dict = {
        "mode": mode,
        "seats": postures or list(COUNCIL_DEFAULT_SEATS),
        "synth_backend": profile.get("synth_backend") or default_synth,
        "profile": seat_profile or None,
        "bounds": bounds,
    }
    if emit_events:
        spec["emit_events"] = True
    return spec


async def _select_face(state: "AgentState") -> tuple[str | None, str | None]:
    """Heuristically choose a face swap based on task content.

    Returns:
        (new_face_id, old_face_id) or (None, None) when no swap is needed.
    """
    task = state.get("task", "")
    current_face = state.get("face_id", "assistant")

    # Dispatch markers always route to the fan-out worker. Scoped work runs on
    # DeepSeek flash v4, every output is reviewed by the MiniMax-M3 critic
    # (overview), and Kimi is the recorded backup — see worker-deepseek.yaml.
    # (No Kimi membership/PAYG bulk split: DeepSeek has no per-account cap, and
    # fan-out width/wave-chunking is handled in dispatch_node, not here.)
    worker_face = "worker-deepseek"

    if state.get("dispatch_subtask") is not None:
        return worker_face, current_face

    phase = state.get("phase")
    if phase in {"dispatch", "execute", "build", "worker"}:
        return worker_face, current_face

    # Planning intent detection
    if _PLAN_INTENT.search(task):
        if _CODE_SHAPED.search(task):
            return "planner-kimi", current_face
        # Concept-shaped planning goes to the senior reasoning tier (MiniMax-M3),
        # not Opus — claude_api is reserved for explicit use to control API cost.
        return "planner-minimax", current_face

    return None, None


async def route_node(state: "AgentState") -> dict:
    """LangGraph node: choose the backend for the current conversation turn."""
    face_id = state.get("face_id", "assistant")
    model_override = state.get("model_override")
    # Honor an explicit face pin (skip the intent heuristic). Set by the gateway for an
    # agent-token turn (the headless contract) or by an opted-in profile (below).
    pin_authoritative = bool(state.get("pin_authoritative"))
    # Hierarchical-managers trigger (NB-W2 A2): the /api/chat ingress can set
    # state["hierarchical"] directly, and an opted-in profile sets it below. When True,
    # _route_after_recall diverts recall → manager_dispatch (the 2-level agent tree).
    # Mirrors pin_authoritative; absent ⇒ byte-identical (no key added to the delta).
    hierarchical = bool(state.get("hierarchical"))

    # ── CoCouncil branch (P1b) ───────────────────────────────────────────────
    # The council-max face routes to its own multi-voice subgraph, NOT to a
    # single backend, so it must short-circuit BEFORE the backend/model override
    # and face-swap paths (a model_override here is a shape hint, not a backend).
    # We set council_spec; _route_after_recall diverts to the council nodes. The
    # `backend` is a harmless placeholder (the council nodes pick per-seat
    # backends themselves). Additive: fires ONLY for face_id == council-max.
    if face_id == _COUNCIL_FACE_ID:
        escalation_backend = "claude_api"
        try:
            from core.faces.registry import get_default_registry
            face = get_default_registry().get_face(face_id)
            escalation_backend = teams.escalation_for(
                face.role, face=face, team=state.get("team")
            )
        except Exception:
            logger.debug(
                "Face registry lookup failed for council face %r", face_id,
                exc_info=True,
            )
        return {
            "backend": "local",
            "face_id": face_id,
            "escalation_backend": escalation_backend,
            "cc_posture": {},
            "council_spec": _build_council_spec(state),
        }

    # ── Profile-driven council (Phase 3a) ────────────────────────────────────
    # A saved profile with a council `shape` runs the council subgraph with its seats
    # + role prompts + bounds — generalizing the council-max branch to any
    # conversation. A profile without a shape is just a roster (use `team=`).
    profile_name = state.get("profile_name")
    if profile_name:
        prof = teams.load_profile(profile_name)
        # A profile can opt into authoritative pinning (interactive "override profile").
        if prof and prof.get("pin_authoritative"):
            pin_authoritative = True
        # A profile can opt into the hierarchical-managers topology (NB-W2 A2). Note: a
        # `shape` profile returns the council subgraph below and takes precedence — the
        # two topologies are mutually exclusive, so a council profile ignores this flag.
        if prof and prof.get("hierarchical"):
            hierarchical = True
        if prof and prof.get("shape"):
            return {
                "backend": "local",
                "face_id": face_id,
                "escalation_backend": "claude_api",
                "cc_posture": {},
                "council_spec": _build_council_spec_from_profile(
                    prof, emit_events=bool(state.get("emit_events"))
                ),
            }
        if prof is None:
            logger.warning("unknown profile %r; ignoring", profile_name)

    # Explicit backend override (request's `backend` field) is the hardest
    # override: the caller has named the backend, so skip face resolution and
    # local discovery entirely. Escalation backend still comes from the face so
    # execute_node's 429 fallback honours face intent.
    backend_override = state.get("backend_override")
    if backend_override:
        from core.config import KNOWN_BACKENDS
        if backend_override not in KNOWN_BACKENDS:
            return {
                "error": (
                    f"Unknown backend {backend_override!r}; "
                    f"valid: {sorted(KNOWN_BACKENDS)}"
                ),
            }
        escalation_backend = "claude_api"
        cc_posture: dict = {}
        try:
            from core.faces.registry import get_default_registry
            face = get_default_registry().get_face(face_id)
            escalation_backend = teams.escalation_for(
                face.role, face=face, team=state.get("team")
            )
            cc_posture = dict(face.cc_posture or {})
        except Exception:
            logger.debug(
                "Face registry lookup failed for face_id=%r on backend-override path",
                face_id,
                exc_info=True,
            )
        return {
            "backend": backend_override,
            "escalation_backend": escalation_backend,
            "cc_posture": cc_posture,
        }

    # Hard override takes top priority — but we still need the face's
    # escalation_backend so execute_node's 429 / NoOpenCodeAvailable
    # fallbacks honour face intent rather than defaulting to kimi_platform.
    if model_override:
        escalation_backend = "claude_api"
        cc_posture = {}
        try:
            from core.faces.registry import get_default_registry
            face = get_default_registry().get_face(face_id)
            escalation_backend = teams.escalation_for(
                face.role, face=face, team=state.get("team")
            )
            cc_posture = dict(face.cc_posture or {})
        except Exception:
            logger.debug(
                "Face registry lookup failed for face_id=%r on override path; "
                "using escalation_backend=%r",
                face_id,
                escalation_backend,
                exc_info=True,
            )
        return {
            "backend": model_override,
            "escalation_backend": escalation_backend,
            "cc_posture": cc_posture,
        }

    # Hierarchical trigger delta (NB-W2 A2): spliced into the face-resolution return
    # paths below so _route_after_recall diverts to manager_dispatch. Empty when the
    # flag is off ⇒ the non-HM return is byte-identical (no extra key). Not added to the
    # council/override early-returns — those are distinct, mutually-exclusive modes.
    _hier = {"hierarchical": True} if hierarchical else {}

    # ── Face swap heuristic ──────────────────────────────────────────────────
    # Headless contract: a vouched agent-token turn (or an interactive profile that
    # sets ``pin_authoritative``) has ALREADY made a vetted face choice — honor the
    # explicit pin and SKIP the intent heuristic. The heuristic exists to infer a face
    # for an UNPINNED interactive user (everyone starts on ``assistant``); it can never
    # return an explicitly-pinned face like ``planner-cc-edit`` (it only yields
    # planner-kimi / planner-minimax / worker-deepseek / None), so without this the
    # cc-edit / BYO-agent surface is structurally unreachable. Pinning only changes
    # face routing — the Gate still enforces the vouched scope — so it grants no new
    # capability.
    if pin_authoritative:
        new_face, old_face = None, None
    else:
        new_face, old_face = await _select_face(state)
    swap_messages: list[dict] = []
    if new_face:
        face_id = new_face
        swap_messages.append(
            {
                "role": "system",
                "content": f"Face swap: '{old_face}' → '{new_face}'",
            }
        )

    # Resolve face preferences
    preferred_backend = "local"
    escalation_backend = "claude_api"
    cc_posture: dict = {}
    try:
        from core.faces.registry import get_default_registry
        face = get_default_registry().get_face(face_id)
        # JOAT v0: resolve (role, context) → backend through the team layer. With
        # NO active team this returns face.preferred_backend / face.escalation_backend
        # byte-for-byte (the regression baseline); a selected team remaps per role.
        team_pin = state.get("team")
        preferred_backend = await teams.resolve(
            face.role, face=face, want_tools=bool(face.allowed_tools), team=team_pin
        )
        escalation_backend = teams.escalation_for(face.role, face=face, team=team_pin)
        cc_posture = dict(face.cc_posture or {})
    except Exception:
        logger.debug(
            "Face registry lookup failed for face_id=%r; falling back to "
            "preferred_backend=%r escalation_backend=%r",
            face_id,
            preferred_backend,
            escalation_backend,
            exc_info=True,
        )

    if preferred_backend == "local":
        backends = await _router.discover()
        best = _router.get_best_backend(backends)
        if best:
            result: dict = {
                "backend": best.name,
                "face_id": face_id,
                "escalation_backend": escalation_backend,
                "cc_posture": cc_posture,
                **_hier,
                "messages": swap_messages + [
                    {
                        "role": "system",
                        "content": f"Routing to local backend: {best.name} ({best.url})",
                    }
                ],
            }
            return result
        # Local unavailable — fall back to escalation backend
        return {
            "backend": escalation_backend,
            "face_id": face_id,
            "escalation_backend": escalation_backend,
            "cc_posture": cc_posture,
            **_hier,
            "messages": swap_messages + [
                {
                    "role": "system",
                    "content": (
                        f"No local backend available for face '{face_id}'. "
                        f"Falling back to {escalation_backend}."
                    ),
                }
            ],
        }

    # ── Workspace validation for workspace-bound workers ─────────────────────
    if face_id == "worker-opencode":
        workspace = state.get("workspace_dir")
        if not workspace:
            return {
                "backend": escalation_backend,
                "face_id": face_id,
                "escalation_backend": escalation_backend,
                "cc_posture": cc_posture,
                **_hier,
                "messages": swap_messages + [
                    {
                        "role": "system",
                        "content": (
                            f"worker-opencode requires workspace_dir; "
                            f"falling back to {escalation_backend}."
                        ),
                    }
                ],
            }

    # Non-local preferred backend (e.g. claude_code, gemini_deep_research)
    return {
        "backend": preferred_backend,
        "face_id": face_id,
        "escalation_backend": escalation_backend,
        "cc_posture": cc_posture,
        **_hier,
        "messages": swap_messages + [
            {
                "role": "system",
                "content": f"Routing to preferred backend: {preferred_backend}",
            }
        ],
    }
