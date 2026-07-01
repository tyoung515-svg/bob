"""BoBClaw Core — JOAT v0 role/team resolver: ``(role, context) → backend``.

A thin generalization of the per-face ``preferred_backend → escalation_backend``
chain behind a swappable **team** config. Faces (and services) request a
*role/lane* — ``apex`` (manager), ``worker`` (bulk), ``critic`` (fast, pinned
local on high-risk) — instead of naming a concrete backend, and a selected team
maps each role to a backend + ordered escalation chain.

PRIME DIRECTIVE — no regression: with **no active team** (the default), the
resolver is a *pure passthrough* — ``resolve`` returns ``face.preferred_backend``
and ``escalation_for`` returns ``face.escalation_backend``, byte-for-byte today's
answer, with NO health-walking (today's ``route_node`` does not pre-probe the
preferred backend; the runtime 429/throttle fallback already lives in
``execute_node`` and is untouched). Teams are opt-in.

Scope (v0, per ``tasks/2026-06-19-joat-v0/INTAKE.md`` resolved decisions):
  * declarative teams + health/throttle fallback only — NO load-based burst (v1).
  * process-local module state, single-process-safe — NO Redis (v1).
  * team config is DATA (backend strings only, never model names).
  * the health-walk MECHANISM ships with an injectable probe seam; the default
    probe is a no-op (returns available), so production behaviour is the declared
    team mapping + the existing runtime fallback. Wiring the probe to live
    per-backend ``health_check()`` / throttle pins is the explicit v1 fill.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

import yaml

from core.config import KNOWN_BACKENDS

if TYPE_CHECKING:  # avoid an import cycle (registry has no need to import teams)
    from core.faces.registry import Face

logger = logging.getLogger(__name__)

Role = Literal["apex", "worker", "critic"]
ROLES: tuple[Role, ...] = ("apex", "worker", "critic")

# Critic-on-high-risk pins here and is never family-swapped (GUI-CU §6). The bare
# "local" backend string is the existing in-process local-model lane.
_LOCAL_BACKEND = "local"


# ── Built-in named teams (DATA — backend strings only, no model names) ──────────
# role → {backend, escalation_chain:[...]}. A role absent from a team falls back
# to the face's own preferred/escalation (graceful). Every backend named here is a
# registered bobclaw-core backend (see config.MAX_WORKER_USD_BY_BACKEND). These are
# example fleets for the routing-view demo + tests; team-EDITING UI is v1. The
# DEFAULT team is the *absence* of a selection (None) → per-face passthrough.
BUILTIN_TEAMS: dict[str, dict[str, dict]] = {
    # Cloud-led fleet: apex on the Claude planning tier, bulk work on a
    # tool-capable cloud worker, critic kept local.
    "cloud-heavy": {
        "apex": {"backend": "claude_code", "escalation_chain": ["claude_api"]},
        "worker": {"backend": "glm_5_2", "escalation_chain": ["deepseek_v4_flash", "kimi_code"]},
        "critic": {"backend": _LOCAL_BACKEND, "escalation_chain": ["minimax"]},
    },
    # Local-first fleet: everything that can run local does; cloud is escalation.
    "local-first": {
        "apex": {"backend": "minimax", "escalation_chain": ["claude_api"]},
        "worker": {"backend": _LOCAL_BACKEND, "escalation_chain": ["deepseek_v4_flash"]},
        "critic": {"backend": _LOCAL_BACKEND, "escalation_chain": ["minimax"]},
    },
    # Centerpiece demo fleet (100-agent run): apex = Claude Opus (claude_api),
    # worker = DeepSeek-Flash (the 100 concurrent workers), critic = GLM-5.2 (the
    # 1:10 chunk-auditor/manager tier). The routing-view under this team renders
    # the three live tiers. NOTE the critic role here is the chunk-AUDITOR fleet,
    # not the per-worker 1:1 critic node (that's a separate reduce stage).
    "demo-fleet": {
        "apex": {"backend": "claude_api", "escalation_chain": ["claude_code"]},
        "worker": {"backend": "deepseek_v4_flash", "escalation_chain": ["glm_5_2", "kimi_code"]},
        "critic": {"backend": "glm_5_2", "escalation_chain": ["minimax"]},
    },
    # Hierarchical-managers fleet (STEERING NB-W1 topology): the top manager +
    # mini-managers are Kimi via its OWN CLI (apex=kimi_cli), bulk workers are
    # DeepSeek-Flash, the first final audit + section critics are GLM. Escalates to
    # kimi_code (HTTP membership) then claude_api if the Kimi CLI is unavailable.
    "hier-fleet": {
        "apex": {"backend": "kimi_cli", "escalation_chain": ["kimi_code", "claude_api"]},
        "worker": {"backend": "deepseek_v4_flash", "escalation_chain": ["glm_5_2"]},
        "critic": {"backend": "glm_5_2", "escalation_chain": ["minimax"]},
    },
}


# ── Custom (user-authored) teams: YAML on disk (DESIGN §6.4 team builder) ───────
# The team-builder creates teams at runtime, so customs live in a writable dir as
# "<slug>.yaml" ({name, roles:{role:{backend, escalation_chain}}}) — NOT in code.
# A shared dir keeps them visible across the multi-process core; they are re-read
# on demand (teams are tiny; resolve runs once per turn). Custom names may not
# shadow a built-in, so the merge stays unambiguous.
_DEFAULT_TEAMS_DIR = Path(__file__).resolve().parent.parent / "data" / "teams"
_custom_teams_dir: Optional[Path] = None
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def set_custom_teams_dir(path: Optional[Path]) -> None:
    """Override the custom-teams dir (tests / deploys). None restores env/default."""
    global _custom_teams_dir
    _custom_teams_dir = Path(path) if path is not None else None


def _teams_dir() -> Path:
    if _custom_teams_dir is not None:
        return _custom_teams_dir
    env = (os.getenv("BOBCLAW_TEAMS_DIR") or "").strip()
    return Path(env) if env else _DEFAULT_TEAMS_DIR


def _as_slots(role_cfg) -> list[dict]:
    """Normalize a role config to a list of ``{name, backend, escalation_chain}`` slots.

    Accepts the legacy single-dict shape OR the multi-slot list shape (a role can bind
    more than one backend); entries without a non-empty string backend are dropped.
    """
    if role_cfg is None:
        return []
    raw = [role_cfg] if isinstance(role_cfg, dict) else role_cfg
    if not isinstance(raw, list):
        return []
    slots: list[dict] = []
    for slot in raw:
        if not isinstance(slot, dict):
            continue
        backend = slot.get("backend")
        if not isinstance(backend, str) or not backend:
            continue
        slots.append({
            "name": str(slot.get("name") or ""),
            "backend": backend,
            "escalation_chain": list(slot.get("escalation_chain") or []),
            "role_prompt": str(slot.get("role_prompt") or ""),
        })
    return slots


def _primary_slot(role_cfg) -> Optional[dict]:
    """The first usable slot for a role — what JOAT v0 ``resolve`` routes to. None ⇒
    unmapped ⇒ passthrough to the face. (Per-subtask selection across the remaining
    roster slots is the dispatch-routing follow-up.)"""
    slots = _as_slots(role_cfg)
    return slots[0] if slots else None


def _normalize_team_cfg(roles: dict) -> dict[str, list]:
    """Coerce a role map to canonical ``{role: [ {name, backend, escalation_chain} ]}``
    (multi-slot). A legacy single-dict role becomes a 1-element list."""
    return {role: _as_slots(cfg) for role, cfg in roles.items()}


def validate_team_config(roles) -> list[str]:
    """Human-readable errors for a proposed role→backend map (empty list = valid).

    Backend STRINGS only — every backend + escalation entry must be a known
    bobclaw-core backend (``config.KNOWN_BACKENDS``); roles ⊆ apex/worker/critic.
    """
    if not isinstance(roles, dict) or not roles:
        return ["team must define at least one role (apex | worker | critic)"]
    errors: list[str] = []
    for role, cfg in roles.items():
        if role not in ROLES:
            errors.append(f"unknown role {role!r} (allowed: {', '.join(ROLES)})")
            continue
        slots = cfg if isinstance(cfg, list) else [cfg]
        if not slots:
            errors.append(f"role {role!r} needs at least one backend slot")
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                errors.append(f"role {role!r} slot must be {{backend, escalation_chain}}")
                continue
            backend = slot.get("backend")
            if not isinstance(backend, str) or not backend:
                errors.append(f"role {role!r} slot needs a backend string")
            elif backend not in KNOWN_BACKENDS:
                errors.append(f"role {role!r} backend {backend!r} is not a known backend")
            for esc in slot.get("escalation_chain") or []:
                if esc not in KNOWN_BACKENDS:
                    errors.append(f"role {role!r} escalation {esc!r} is not a known backend")
            rp = slot.get("role_prompt")
            if rp is not None and not isinstance(rp, str):
                errors.append(f"role {role!r} role_prompt must be a string")
    return errors


def _parse_custom_teams(directory: Path) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    if not directory.exists():
        return out
    for f in sorted(directory.glob("*.yaml")):
        slug = f.stem
        if slug in BUILTIN_TEAMS:
            continue  # a built-in always wins; never shadow it
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            roles = data.get("roles") if isinstance(data, dict) else None
            if not isinstance(roles, dict) or validate_team_config(roles):
                raise ValueError("missing or invalid 'roles' map")
            out[slug] = _normalize_team_cfg(roles)
        except Exception:
            logger.warning("skipping malformed team file %s", f, exc_info=True)
    return out


def _load_custom_teams() -> dict[str, dict[str, dict]]:
    """Custom teams parsed from the teams dir (malformed files skipped)."""
    return _parse_custom_teams(_teams_dir())


def _all_teams() -> dict[str, dict[str, dict]]:
    """Built-in + custom teams. Customs cannot shadow a built-in (create_team and
    the loader both reject that), so a plain merge is unambiguous."""
    merged: dict[str, dict[str, dict]] = dict(BUILTIN_TEAMS)
    merged.update(_load_custom_teams())
    return merged


def known_teams() -> list[str]:
    """Names of the selectable teams (built-in + custom), excluding the default."""
    return sorted(_all_teams())


def list_teams() -> list[dict]:
    """Every selectable team with its role→backend config + a ``builtin`` flag —
    the read model for the team-builder and GET /api/teams."""
    custom = _load_custom_teams()
    out = []
    for name in sorted({*BUILTIN_TEAMS, *custom}):
        cfg = custom.get(name) or BUILTIN_TEAMS.get(name) or {}
        out.append({
            "name": name,
            "builtin": name in BUILTIN_TEAMS,
            "roles": {role: _as_slots(rc) for role, rc in cfg.items()},
        })
    return out


def create_team(name: str, roles: dict, *, overwrite: bool = False) -> dict:
    """Validate and persist a custom team as YAML. Returns ``{name, builtin, roles}``.

    Raises ValueError (human message) on a bad name, a built-in-name collision, an
    invalid role config, or an existing team when ``overwrite`` is False.
    """
    slug = (name or "").strip().lower()
    if not slug:
        raise ValueError("team name is required")
    if not _SLUG_RE.match(slug):
        raise ValueError("team name must be a lowercase slug (a-z, 0-9, hyphens)")
    if slug in BUILTIN_TEAMS:
        raise ValueError(f"{slug!r} is a built-in team; choose another name")
    errors = validate_team_config(roles)
    if errors:
        raise ValueError("; ".join(errors))
    norm = _normalize_team_cfg(roles)
    directory = _teams_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.yaml"
    if path.exists() and not overwrite:
        raise ValueError(f"team {slug!r} already exists")
    path.write_text(
        yaml.safe_dump({"name": slug, "roles": norm}, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("custom team %r saved to %s", slug, path)
    return {"name": slug, "builtin": False, "roles": norm}


def delete_team(name: str) -> bool:
    """Delete a custom team file. Returns True if removed. Built-ins are immutable."""
    slug = (name or "").strip().lower()
    if slug in BUILTIN_TEAMS:
        raise ValueError(f"{slug!r} is a built-in team and cannot be deleted")
    path = _teams_dir() / f"{slug}.yaml"
    if path.exists():
        path.unlink()
        logger.info("custom team %r deleted", slug)
        return True
    return False


# ── Profiles: a team is the WHO; a profile adds the HOW ─────────────────────────
# Same YAML store, superset schema — a team is a profile with empty role-prompts
# and no shape/seats/schedule. The routing path (resolve / known_teams /
# _parse_custom_teams) reads only `roles` and is UNCHANGED; the profile-only keys
# (role_prompt per slot, shape, seats, protocol_bounds, schedule) are read by
# load_profile / list_profiles — and, later, by route / panel / grounding.
# See plans/virtual-hatching-widget.md.
_PROFILE_SHAPES: frozenset[str] = frozenset({"fusion", "sequential", "debate"})


def _as_seats(raw) -> list[dict]:
    """Normalize a council ``seats`` list → ``[{posture, backend?, fallback_chain,
    role_prompt}]``. Entries without a string posture are dropped; ``backend`` is
    optional (a seat may inherit the posture's default backend at resolve time)."""
    if not isinstance(raw, list):
        return []
    seats: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        posture = s.get("posture")
        if not isinstance(posture, str) or not posture:
            continue
        seat = {
            "posture": posture,
            "fallback_chain": list(s.get("fallback_chain") or []),
            "role_prompt": str(s.get("role_prompt") or ""),
        }
        backend = s.get("backend")
        if isinstance(backend, str) and backend:
            seat["backend"] = backend
        seats.append(seat)
    return seats


def validate_profile(profile) -> list[str]:
    """Human-readable errors for a full profile envelope (empty list = valid).

    A profile must define a ``roles`` roster and/or ``seats``. Optional ``shape``,
    ``synth_backend``, ``protocol_bounds``, ``schedule`` are validated when present.
    Reuses :func:`validate_team_config` for the roster.
    """
    if not isinstance(profile, dict):
        return ["profile must be an object"]
    errors: list[str] = []
    roles = profile.get("roles")
    seats = profile.get("seats")
    if not roles and not seats:
        errors.append("profile must define `roles` and/or `seats`")
    if roles:
        errors.extend(validate_team_config(roles))
    if seats is not None:
        if not isinstance(seats, list):
            errors.append("`seats` must be a list")
        else:
            for s in seats:
                if not isinstance(s, dict) or not isinstance(s.get("posture"), str):
                    errors.append("each seat needs a `posture` string")
                    continue
                b = s.get("backend")
                if b is not None and b not in KNOWN_BACKENDS:
                    errors.append(f"seat {s['posture']!r} backend {b!r} is not a known backend")
                for esc in s.get("fallback_chain") or []:
                    if esc not in KNOWN_BACKENDS:
                        errors.append(f"seat {s['posture']!r} fallback {esc!r} is not a known backend")
                rp = s.get("role_prompt")
                if rp is not None and not isinstance(rp, str):
                    errors.append(f"seat {s['posture']!r} role_prompt must be a string")
    shape = profile.get("shape")
    if shape is not None and shape not in _PROFILE_SHAPES:
        errors.append(f"unknown shape {shape!r} (allowed: {', '.join(sorted(_PROFILE_SHAPES))})")
    synth = profile.get("synth_backend")
    if synth is not None and synth not in KNOWN_BACKENDS:
        errors.append(f"synth_backend {synth!r} is not a known backend")
    bounds = profile.get("protocol_bounds")
    if bounds is not None:
        if not isinstance(bounds, dict):
            errors.append("`protocol_bounds` must be an object")
        else:
            errors.extend(_validate_protocol_bounds(bounds))
    if profile.get("schedule") is not None and not isinstance(profile["schedule"], dict):
        errors.append("`schedule` must be an object")
    hier = profile.get("hierarchical")
    if hier is not None and not isinstance(hier, bool):
        errors.append("`hierarchical` must be a boolean")
    return errors


def _validate_protocol_bounds(bounds: dict) -> list[str]:
    """Value-level checks for a profile's ``protocol_bounds`` (P3b/P5 hardening):
    catch a misconfigured bound at AUTHOR time instead of relying on the runtime's
    fail-safe coercion. Rejects clearly-wrong values; a positive-but-sub-spawn
    ``max_usd`` is allowed (the grounding ceiling notice flags it at runtime, and a
    tiny intentional ceiling is a valid demo)."""
    errs: list[str] = []

    def _is_num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if "max_usd" in bounds:
        mu = bounds["max_usd"]
        if not _is_num(mu) or mu <= 0:
            errs.append("`protocol_bounds.max_usd` must be a positive number")
    if "restart_budget" in bounds:
        rb = bounds["restart_budget"]
        if not isinstance(rb, int) or isinstance(rb, bool) or rb < 0:
            errs.append("`protocol_bounds.restart_budget` must be a non-negative integer")
    if "max_rounds" in bounds:
        mr = bounds["max_rounds"]
        if not isinstance(mr, int) or isinstance(mr, bool) or mr < 1:
            errs.append("`protocol_bounds.max_rounds` must be a positive integer")
        else:
            # Cap so a debate's super-step budget (~4 per round) stays under the
            # graph recursion limit — fail the author loudly here, not mid-run.
            from core.config import COUNCIL_MAX_ROUNDS_CEILING
            if mr > COUNCIL_MAX_ROUNDS_CEILING:
                errs.append(f"`protocol_bounds.max_rounds` must be <= "
                            f"{COUNCIL_MAX_ROUNDS_CEILING} (graph recursion budget)")
    if "drift_threshold" in bounds:
        dt = bounds["drift_threshold"]
        if not _is_num(dt) or not (0 <= dt <= 1):
            errs.append("`protocol_bounds.drift_threshold` must be a number between 0 and 1")
    g = bounds.get("grounding")
    if g is not None and not isinstance(g, bool) and not _is_num(g):
        # A string must be a recognized on/off token (the SAME allowlist the runtime
        # coerces against). bool / numeric are accepted (runtime handles them).
        from core.nodes.grounding import GROUNDING_OFF_TOKENS, GROUNDING_ON_TOKENS
        if str(g).strip().lower() not in (*GROUNDING_ON_TOKENS, *GROUNDING_OFF_TOKENS):
            errs.append(f"`protocol_bounds.grounding` {g!r} is not a recognized on/off value")
    return errs


def _normalize_profile(name: str, profile: dict) -> dict:
    """Canonical profile envelope written to disk: name + normalized roles/seats +
    only the profile-keys that are set (a plain team stays ``{name, roles}``)."""
    env: dict = {"name": name}
    if profile.get("roles"):
        env["roles"] = _normalize_team_cfg(profile["roles"])
    if profile.get("seats"):
        env["seats"] = _as_seats(profile["seats"])
    for key in ("shape", "synth_backend", "protocol_bounds", "schedule", "pin_authoritative", "hierarchical"):
        if profile.get(key):
            env[key] = profile[key]
    return env


def create_profile(name: str, profile: dict, *, overwrite: bool = False) -> dict:
    """Validate and persist a full profile (the superset of a team). Returns the
    normalized envelope + ``builtin: False``. Raises ValueError on a bad name, a
    built-in collision, an invalid envelope, or an existing profile."""
    slug = (name or "").strip().lower()
    if not slug:
        raise ValueError("profile name is required")
    if not _SLUG_RE.match(slug):
        raise ValueError("profile name must be a lowercase slug (a-z, 0-9, hyphens)")
    if slug in BUILTIN_TEAMS:
        raise ValueError(f"{slug!r} is a built-in team; choose another name")
    errors = validate_profile(profile)
    if errors:
        raise ValueError("; ".join(errors))
    env = _normalize_profile(slug, profile)
    directory = _teams_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.yaml"
    if path.exists() and not overwrite:
        raise ValueError(f"profile {slug!r} already exists")
    path.write_text(yaml.safe_dump(env, sort_keys=False), encoding="utf-8")
    logger.info("profile %r saved to %s", slug, path)
    return {**env, "builtin": False}


def load_profile(name: str) -> Optional[dict]:
    """Full profile envelope for a built-in or custom team. None if unknown.

    Built-ins surface as ``{name, builtin:True, roles}`` (no shape/seats). Custom
    files surface every set key (roles / seats / shape / synth_backend /
    protocol_bounds / schedule). Roles + seats are normalized via _as_slots/_as_seats.
    """
    slug = (name or "").strip().lower()
    if slug in BUILTIN_TEAMS:
        return {
            "name": slug,
            "builtin": True,
            "roles": {role: _as_slots(rc) for role, rc in BUILTIN_TEAMS[slug].items()},
        }
    path = _teams_dir() / f"{slug}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("failed to read profile %s", path, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    env: dict = {"name": slug, "builtin": False}
    if isinstance(data.get("roles"), dict):
        env["roles"] = {role: _as_slots(rc) for role, rc in data["roles"].items()}
    if data.get("seats"):
        env["seats"] = _as_seats(data["seats"])
    for key in ("shape", "synth_backend", "protocol_bounds", "schedule", "pin_authoritative", "hierarchical"):
        if data.get(key):
            env[key] = data[key]
    return env


def list_profiles() -> list[dict]:
    """Full profile envelopes for every selectable team/profile (built-in + custom)."""
    directory = _teams_dir()
    custom = {p.stem for p in directory.glob("*.yaml")} if directory.exists() else set()
    names = sorted({*BUILTIN_TEAMS, *(n for n in custom if n not in BUILTIN_TEAMS)})
    return [env for env in (load_profile(n) for n in names) if env is not None]


# ── Process-local active-team state (single-process-safe; v0, NO Redis yet) ─────
# A programmatic override; when None the env var BOBCLAW_TEAM is consulted live so
# the routing-view reflects an env change without a restart (P2 demo).
_active_team: Optional[str] = None


def set_active_team(name: Optional[str]) -> None:
    """Set the process-level active team. ``None`` restores the default (per-face).

    Raises ValueError on an unknown team so a typo fails loud rather than silently
    falling back to the default fleet.
    """
    global _active_team
    if name is not None and name not in _all_teams():
        raise ValueError(f"Unknown team {name!r}; known: {known_teams()}")
    _active_team = name


def get_active_team() -> Optional[str]:
    """Resolve the process-level active team: explicit set > ``BOBCLAW_TEAM`` > None.

    An unknown ``BOBCLAW_TEAM`` value warns and degrades to the default rather
    than erroring (env is operator-set at launch; a bad value must not wedge the
    process).
    """
    if _active_team is not None:
        return _active_team
    env = (os.getenv("BOBCLAW_TEAM") or "").strip()
    if not env:
        return None
    if env not in _all_teams():
        logger.warning("BOBCLAW_TEAM=%r is not a known team; using the default fleet", env)
        return None
    return env


# ── Health-walk seam (injectable; default no-op → JOAT v0 passthrough) ──────────
# A backend is "available" unless this probe says otherwise. The DEFAULT probe is a
# no-op (always True) so an un-wired process behaves exactly like JOAT v0 — the
# regression baseline — and tests that don't install a probe see no health-walking.
#
# JOAT v1 installs a LIVE probe (per-backend health_check() + Redis throttle pins,
# cached + fail-open) via ``set_health_probe`` at server startup
# (start.py._on_startup → core.health_probe.install_live_probe). The live probe lives
# OUTSIDE this module on purpose: teams.py stays DATA-only (no backend-client / Redis
# imports — keeps test_no_model_names_in_core and the import-light convention intact).
# Tests still swap the probe directly (``teams._health_probe = ...``) to drive the walk.
async def _default_health_probe(backend: str) -> bool:  # noqa: ARG001 - seam
    return True


_health_probe: Callable[[str], Awaitable[bool]] = _default_health_probe


def set_health_probe(probe: Callable[[str], Awaitable[bool]]) -> None:
    """Install the availability probe ``resolve`` walks the escalation chain against.

    Production wires the live probe here at startup; passing ``_default_health_probe``
    restores the no-op (JOAT v0) behaviour. Kept a tiny setter so callers never import
    a concrete probe symbol from this DATA-only module.
    """
    global _health_probe
    _health_probe = probe


def health_probe_is_live() -> bool:
    """True once a non-default probe is installed (the routing-view ``live_probe``
    flag: under the no-op default the resolved backend is the DECLARED mapping, not a
    health-checked one)."""
    return _health_probe is not _default_health_probe


async def _is_available(backend: str) -> bool:
    """Best-effort availability check. Fail-open: a raising probe → available."""
    try:
        return await _health_probe(backend)
    except Exception:  # pragma: no cover - defensive
        logger.debug("health probe raised for %r; assuming available", backend, exc_info=True)
        return True


def _team_role_cfg(team_name: Optional[str], role: Optional[str]) -> Optional[dict]:
    """The ``{backend, escalation_chain}`` for (team, role), or None.

    None ⇒ default behaviour (no team, no role, or the team doesn't map this role)
    ⇒ callers passthrough to the face's own preferred/escalation.
    """
    if not team_name or not role:
        return None
    return _all_teams().get(team_name, {}).get(role)


async def resolve(
    role: Optional[str],
    *,
    face: "Face",
    want_tools: bool = False,
    risk: str = "normal",
    team: Optional[str] = None,
) -> str:
    """Resolve ``(role, context) → backend`` for the current turn.

    Args:
        role: the face's role (apex|worker|critic) or None.
        face: the active Face (the passthrough source for the default team).
        want_tools: when True, prefer a tool-capable backend in the chain.
        risk: "normal" | "high". ``critic`` + "high" is pinned local.
        team: per-conversation pin. Precedence: ``team`` > ``BOBCLAW_TEAM`` > none.

    Returns the concrete backend string.

    DEFAULT team (no team active and no pin): a pure passthrough to
    ``face.preferred_backend`` — no probing, byte-for-byte today's answer.
    ACTIVE team: ``team[role].backend`` walking the escalation chain on
    unhealthy/throttled, honouring ``want_tools`` and the critic@high-risk pin.
    """
    effective_team = team or get_active_team()
    primary = _primary_slot(_team_role_cfg(effective_team, role))

    # ── DEFAULT team / unmapped role: pure passthrough (the regression baseline) ──
    if primary is None:
        return face.preferred_backend

    # ── ACTIVE team — route to the role's PRIMARY slot. Multi-slot rosters resolve to
    # slot[0]; per-subtask selection across the rest is the dispatch-routing follow-up.
    # Critic on high-risk is pinned local — never family-swapped (GUI-CU §6).
    if role == "critic" and risk == "high":
        return _LOCAL_BACKEND

    candidates = [primary["backend"], *primary["escalation_chain"]]

    # Tool-capability preference: float tool-capable backends to the front, but
    # never empty the chain (a non-tool fleet still resolves to *something*).
    if want_tools:
        from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS

        tool_first = [b for b in candidates if b in TOOL_CAPABLE_BACKENDS]
        rest = [b for b in candidates if b not in TOOL_CAPABLE_BACKENDS]
        candidates = tool_first + rest

    # Walk the chain: first available wins. Whole chain down → return the primary
    # and let execute_node's runtime fallbacks handle it (same as today).
    for backend in candidates:
        if await _is_available(backend):
            return backend
    return candidates[0]


def role_backend(team: Optional[str], role: Role) -> Optional[str]:
    """The primary backend a team binds to *role*, or None (no team / role unmapped).

    A thin, Face-free public read over the team config — for callers that resolve a
    role backend WITHOUT a Face and need MORE THAN ONE role within a single turn
    (e.g. the build pipeline's apex/worker split: plan_contracts wants the apex
    backend while the fan-out wants the worker backend). Honours the active-team
    fallback (programmatic set > ``BOBCLAW_TEAM`` > none) like :func:`resolve`, but
    does NOT health-walk the chain (a one-shot read; the runtime 429 fallback in
    execute_node still applies). None ⇒ the caller should passthrough to its own
    default (typically ``state["backend"]``)."""
    effective_team = team or get_active_team()
    primary = _primary_slot(_team_role_cfg(effective_team, role))
    return primary["backend"] if primary else None


def escalation_for(
    role: Optional[str],
    *,
    face: "Face",
    team: Optional[str] = None,
) -> str:
    """The escalation backend to stash in state for ``execute_node``'s 429/throttle
    fallback. Default team → ``face.escalation_backend`` (byte-for-byte today).
    Active team → the first hop of the role's escalation chain (or the face's
    escalation if the team declares none).
    """
    effective_team = team or get_active_team()
    primary = _primary_slot(_team_role_cfg(effective_team, role))
    if primary is None:
        return face.escalation_backend
    chain = primary["escalation_chain"]
    return chain[0] if chain else face.escalation_backend


def escalation_chain(
    role: Optional[str],
    *,
    face: "Face",
    team: Optional[str] = None,
) -> list[str]:
    """Full ordered escalation chain for (role, team) — for the routing-view.

    Default team → ``[face.escalation_backend]``. Active team → the declared chain
    (or the face's escalation when the team declares none).
    """
    effective_team = team or get_active_team()
    primary = _primary_slot(_team_role_cfg(effective_team, role))
    if primary is None:
        return [face.escalation_backend]
    chain = list(primary["escalation_chain"])
    return chain or [face.escalation_backend]
