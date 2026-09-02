"""T1 flight-aware routing (Lane 1b) — the namespaced fast-path glue.

The pieces that make the T1 shortcut-profile fast path FLIGHT-AWARE and CONCURRENCY-SAFE,
wired conservatively into ``route_node``'s ``_select_face`` fallback:

  * **job-shape** — a coarse key for the turn, derived from the SAME signals ``_select_face``
    uses (dispatch/phase → worker; plan+code → plan_code; plan → plan_concept; else chat), so
    a profile is keyed by what kind of job this is.
  * **two-tier namespacing** — a profile key is scoped ``proj:<project>:<job_shape>`` first,
    with a ``global:<job_shape>`` fallback. Fixes the T1 cross-contamination (one project's
    learned routing never silently drives another); a profile is promoted to the global tier
    only after cross-project consensus (see ``core.mcp_tools.profile_consensus``).
  * **ε exploration valve (F1)** — on a servable hit, with probability ε (default 0.10) SKIP
    the fast path and fall back to retrieval/heuristic, so a crystallized profile can never
    fully suppress exploration. The ε rate + blast-radius policy is SES-tunable (SPEC §8).
  * **class→face binding** — a profile binds to a capability-CLASS (survives a model swap);
    for v1 the class resolves to a face via an explicit, reviewable map (``_CLASS_FACE``),
    unknown ⇒ None ⇒ fall back to the heuristic (never a blind guess into the core routing).

PURE (the RNG + cache are injected). ``resolve_fastpath_face`` returns None whenever the
fast path should not fire — so a caller that always falls back on None is byte-identical.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from core.mcp_tools.profiles import ShortcutProfile

# Reuse the _select_face intent signals so a profile's job-shape matches what the heuristic
# would key on (kept local to avoid importing route.py — a cycle; regexes mirror route.py).
_PLAN_INTENT = re.compile(
    r"plan|design|architect|spec|roadmap|decompose|break down|figure out how|propose|"
    r"outline|map out|scope", re.I,
)
_CODE_SHAPED = re.compile(
    r"refactor|migrate|implement|integrate|repo|module|class|function|endpoint|api|test|"
    r"build|compile|deploy|pipeline|service|backend|frontend|schema|sql|crud|fix|patch|"
    r"bug|merge|pr|commit|library|package|dependency", re.I,
)

# Job-shape keys (coarse, stable).
JOB_WORKER = "worker"
JOB_PLAN_CODE = "plan_code"
JOB_PLAN_CONCEPT = "plan_concept"
JOB_CHAT = "chat"

# Namespace tiers.
_PROJECT_DEFAULT = "default"


def job_shape_for(state: dict) -> str:
    """Coarse job-shape for a turn, from the same signals ``_select_face`` reads."""
    if state.get("dispatch_subtask") is not None:
        return JOB_WORKER
    if state.get("phase") in {"dispatch", "execute", "build", "worker"}:
        return JOB_WORKER
    task = state.get("task", "") or ""
    if _PLAN_INTENT.search(task):
        return JOB_PLAN_CODE if _CODE_SHAPED.search(task) else JOB_PLAN_CONCEPT
    return JOB_CHAT


def project_of(state: dict) -> str:
    """The project namespace for a turn. Explicit ``state['project']`` wins; else the
    default tier. (Project threading is orthogonal; the namespacing MECHANISM is here.)"""
    p = state.get("project")
    return str(p).strip() if (isinstance(p, str) and p.strip()) else _PROJECT_DEFAULT


def namespaced_key(job_shape: str, project: str) -> str:
    """Per-project profile key: ``proj:<project>:<job_shape>``."""
    return f"proj:{project}:{job_shape}"


def global_key(job_shape: str) -> str:
    """Cross-project (promoted) profile key: ``global:<job_shape>``."""
    return f"global:{job_shape}"


def epsilon_should_explore(rate: float, roll: float) -> bool:
    """True ⇒ SKIP the fast path this turn (explore). ``roll`` is an injected [0,1) draw
    (the caller passes ``random.random()`` in prod; tests pass a fixed value). Clamps rate."""
    r = 0.0 if rate < 0 else 1.0 if rate > 1 else rate
    return roll < r


# ── capability-class → face (v1: explicit, reviewable; unknown ⇒ heuristic) ────
# Conservative seed. A profile binds to a CLASS, not a model; this maps the class to a face
# whose team-role then resolves the backend (teams.resolve). Unknown class ⇒ None ⇒ the
# caller keeps the _select_face heuristic (never a blind route). SES-tunable (SPEC §8: a
# thin capability-class→backend registry is the follow-on; until then profiles bind to
# these faces / their JOAT roles).
_CLASS_FACE: dict[str, str] = {
    "worker": "worker-deepseek",
    "route_cheap": "worker-deepseek",
    "plan_code": "planner-kimi",
    "synth_mid": "planner-minimax",
    "plan_concept": "planner-minimax",
    "synth_deep": "planner-minimax",
}


def class_to_face(capability_class: Optional[str]) -> Optional[str]:
    """Resolve a profile's capability-class to a face for v1, or None (⇒ fall back to the
    heuristic). A class that is already a known face-shaped token maps to itself only if
    listed; anything unknown returns None (safe)."""
    if not capability_class:
        return None
    return _CLASS_FACE.get(str(capability_class).strip())


async def lookup_two_tier(cache, job_shape: str, project: str):
    """Two-tier servable lookup: per-project first, then the global tier. ``cache`` is any
    object exposing async ``lookup(key) -> Optional[ShortcutProfile]`` (``RedisProfileCache``,
    in-process-only when its redis getter is None). Returns the servable profile or None."""
    hit = await cache.lookup(namespaced_key(job_shape, project))
    if hit is not None:
        return hit
    return await cache.lookup(global_key(job_shape))


async def resolve_fastpath_face(
    state: dict,
    cache,
    *,
    epsilon: float,
    roll: Callable[[], float],
) -> Optional[str]:
    """The whole fast path in one call: derive the job-shape + project, two-tier-lookup a
    servable profile, apply the ε valve, and resolve its class → face. Returns the face to
    use, or None whenever the fast path should NOT fire (no hit / ε explores / unknown class /
    resolved face equals the current face). A None return ⇒ the caller keeps _select_face, so
    the routing is byte-identical unless a servable profile genuinely wins."""
    job_shape = job_shape_for(state)
    project = project_of(state)
    profile: Optional[ShortcutProfile] = await lookup_two_tier(cache, job_shape, project)
    if profile is None:
        return None
    if epsilon_should_explore(epsilon, roll()):
        return None
    face = class_to_face(profile.capability_class)
    if not face or face == state.get("face_id"):
        return None
    return face


__all__ = [
    "JOB_WORKER", "JOB_PLAN_CODE", "JOB_PLAN_CONCEPT", "JOB_CHAT",
    "job_shape_for", "project_of", "namespaced_key", "global_key",
    "epsilon_should_explore", "class_to_face", "lookup_two_tier",
    "resolve_fastpath_face",
]
