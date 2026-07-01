"""JOAT v0 — tests for the (role, context) → backend resolver (``core/teams.py``).

The PRIME DIRECTIVE under test: with no active team the resolver reproduces today's
per-face ``preferred_backend → escalation_backend`` answer byte-for-byte. Teams are
opt-in; selecting one measurably changes resolution; critic@high-risk pins local;
the escalation chain walks on unhealthy backends.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import teams
from core.faces.registry import Face, get_default_registry


# ── isolation: reset process-local state + the health probe between tests ───────
@pytest.fixture(autouse=True)
def _reset_team_state():
    teams.set_active_team(None)
    original_probe = teams._health_probe
    yield
    teams.set_active_team(None)
    teams._health_probe = original_probe
    teams.set_custom_teams_dir(None)


def _face(**kw) -> Face:
    base = dict(id="t", name="T", system_prompt="p")
    base.update(kw)
    return Face(**base)


# ── DEFAULT TEAM = pure passthrough (the regression baseline) ───────────────────

@pytest.mark.asyncio
async def test_default_team_resolve_is_passthrough_for_every_real_face():
    """No team selected → resolve == the face's own preferred_backend, for ALL
    17 live faces. This is the no-regression contract, asserted exhaustively."""
    faces = get_default_registry().all_faces()
    assert len(faces) >= 17  # guard: caught if a profile silently disappears
    for face in faces:
        got = await teams.resolve(face.role, face=face)
        assert got == face.preferred_backend, (
            f"{face.id}: default-team resolve drifted "
            f"({got!r} != preferred {face.preferred_backend!r})"
        )


def test_default_team_escalation_matches_every_real_face():
    faces = get_default_registry().all_faces()
    for face in faces:
        assert teams.escalation_for(face.role, face=face) == face.escalation_backend
        assert teams.escalation_chain(face.role, face=face) == [face.escalation_backend]


@pytest.mark.asyncio
async def test_default_team_ignores_role_want_tools_and_risk():
    """Passthrough is unconditional in the default team — want_tools / risk / a
    set role never perturb it (they only bite under an active team)."""
    face = _face(preferred_backend="local", escalation_backend="minimax", role="worker")
    assert await teams.resolve("worker", face=face, want_tools=True) == "local"
    assert await teams.resolve("critic", face=face, risk="high") == "local"
    assert await teams.resolve(None, face=face) == "local"


# ── ACTIVE TEAM = role-mapped resolution ────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_team_remaps_worker_and_apex():
    face = _face(preferred_backend="local", escalation_backend="claude_api", role="worker")
    teams.set_active_team("cloud-heavy")
    # worker role → cloud-heavy worker backend, NOT the face's preferred.
    assert await teams.resolve("worker", face=face) == "glm_5_2"
    # apex role → cloud-heavy apex backend.
    assert await teams.resolve("apex", face=face) == "claude_code"
    # escalation now follows the team's chain head, not the face.
    assert teams.escalation_for("worker", face=face) == "deepseek_v4_flash"


@pytest.mark.asyncio
async def test_unmapped_role_falls_back_to_face_under_active_team():
    """A role the team doesn't declare → graceful passthrough to the face."""
    face = _face(preferred_backend="kimi_code", escalation_backend="claude_api", role=None)
    teams.set_active_team("cloud-heavy")
    assert await teams.resolve(None, face=face) == "kimi_code"  # role None → no map


# ── per-conversation pin precedence: pin > env > default ────────────────────────

@pytest.mark.asyncio
async def test_per_conversation_pin_overrides_process_team():
    face = _face(preferred_backend="local", role="apex")
    teams.set_active_team("local-first")  # process team → apex=minimax
    assert await teams.resolve("apex", face=face) == "minimax"
    # explicit per-conversation pin wins over the process team.
    assert await teams.resolve("apex", face=face, team="cloud-heavy") == "claude_code"


@pytest.mark.asyncio
async def test_bobclaw_team_env_selects_team(monkeypatch):
    monkeypatch.setenv("BOBCLAW_TEAM", "cloud-heavy")
    face = _face(preferred_backend="local", role="worker")
    assert teams.get_active_team() == "cloud-heavy"
    assert await teams.resolve("worker", face=face) == "glm_5_2"


@pytest.mark.asyncio
async def test_unknown_bobclaw_team_env_degrades_to_default(monkeypatch, caplog):
    monkeypatch.setenv("BOBCLAW_TEAM", "does-not-exist")
    face = _face(preferred_backend="local", role="worker")
    assert teams.get_active_team() is None
    assert await teams.resolve("worker", face=face) == "local"  # passthrough


def test_set_active_team_unknown_raises():
    with pytest.raises(ValueError, match="Unknown team"):
        teams.set_active_team("nope")


# ── critic @ high-risk is pinned local, never family-swapped ────────────────────

@pytest.mark.asyncio
async def test_critic_high_risk_pinned_local(monkeypatch):
    """Even when the active team maps critic to a NON-local backend, risk='high'
    forces local. risk='normal' uses the team's critic backend."""
    monkeypatch.setitem(
        teams.BUILTIN_TEAMS,
        "_test-cloud-critic",
        {"critic": {"backend": "minimax", "escalation_chain": ["claude_api"]}},
    )
    face = _face(preferred_backend="local", role="critic")
    teams.set_active_team("_test-cloud-critic")
    assert await teams.resolve("critic", face=face, risk="high") == "local"
    assert await teams.resolve("critic", face=face, risk="normal") == "minimax"


# ── health-fallback walks the escalation chain ──────────────────────────────────

@pytest.mark.asyncio
async def test_health_fallback_walks_chain(monkeypatch):
    """Primary unhealthy → resolve returns the first healthy escalation hop."""
    async def probe(backend: str) -> bool:
        return backend != "glm_5_2"  # cloud-heavy worker primary is "down"

    monkeypatch.setattr(teams, "_health_probe", probe)
    face = _face(preferred_backend="local", role="worker")
    teams.set_active_team("cloud-heavy")
    # chain = [glm_5_2(down), deepseek_v4_flash, kimi_code] → first healthy.
    assert await teams.resolve("worker", face=face) == "deepseek_v4_flash"


@pytest.mark.asyncio
async def test_health_fallback_whole_chain_down_returns_primary(monkeypatch):
    async def probe(backend: str) -> bool:
        return False

    monkeypatch.setattr(teams, "_health_probe", probe)
    face = _face(preferred_backend="local", role="worker")
    teams.set_active_team("cloud-heavy")
    assert await teams.resolve("worker", face=face) == "glm_5_2"  # primary


@pytest.mark.asyncio
async def test_probe_failure_fails_open(monkeypatch):
    async def probe(backend: str) -> bool:
        raise RuntimeError("probe boom")

    monkeypatch.setattr(teams, "_health_probe", probe)
    face = _face(preferred_backend="local", role="worker")
    teams.set_active_team("cloud-heavy")
    assert await teams.resolve("worker", face=face) == "glm_5_2"  # assumed available


# ── want_tools prefers a tool-capable backend in the chain ──────────────────────

@pytest.mark.asyncio
async def test_want_tools_floats_tool_capable_backend():
    """local-first worker primary 'local' is not tool-capable; its escalation
    'deepseek_v4_flash' is → want_tools floats it to the front."""
    from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS

    assert "deepseek_v4_flash" in TOOL_CAPABLE_BACKENDS
    assert "local" not in TOOL_CAPABLE_BACKENDS
    face = _face(preferred_backend="kimi_code", role="worker")
    teams.set_active_team("local-first")
    assert await teams.resolve("worker", face=face, want_tools=False) == "local"
    assert await teams.resolve("worker", face=face, want_tools=True) == "deepseek_v4_flash"


# ── misc surface ────────────────────────────────────────────────────────────────

def test_known_teams_lists_builtins(teams_dir):
    # Isolate the teams dir (empty tmp) so the assertion is robust to any
    # locally-saved custom teams/profiles in the real data/teams dir (e.g. the
    # `premium-build` example) — known_teams() merges builtins + the dir.
    assert teams.known_teams() == ["cloud-heavy", "demo-fleet", "hier-fleet", "local-first"]


@pytest.mark.asyncio
async def test_demo_fleet_resolves_three_tiers():
    """The centerpiece fleet: apex→claude_api (Opus), worker→deepseek_v4_flash,
    critic→glm_5_2 (the GLM chunk-auditor tier)."""
    face = _face(preferred_backend="local", escalation_backend="claude_api")
    teams.set_active_team("demo-fleet")
    assert await teams.resolve("apex", face=face) == "claude_api"
    assert await teams.resolve("worker", face=face) == "deepseek_v4_flash"
    assert await teams.resolve("critic", face=face) == "glm_5_2"
    assert teams.escalation_chain("worker", face=face) == ["glm_5_2", "kimi_code"]


def test_joat_surface_contains_no_model_names():
    """Team config + the role: profile additions are DATA = backend strings only.
    Mirror the core/memory no-model-names guard across the ENTIRE JOAT surface
    (teams.py, registry.py, every face profile YAML) so a model id can never creep
    in via any of them — closes the coverage gap the verification flagged."""
    forbidden = [
        "granite", "gemma", "qwen3", "qwen2", "nomic", "bge-m3",
        "llama-", "claude-3", "claude-4", "gpt-4", "gpt-3.5",
    ]
    import core.faces.registry as _registry

    targets = [Path(teams.__file__), Path(_registry.__file__)]
    targets += sorted((Path(_registry.__file__).parent / "profiles").glob("*.yaml"))
    violations: list[str] = []
    for path in targets:
        lower = path.read_text(encoding="utf-8").lower()
        for tok in forbidden:
            if tok in lower:
                violations.append(f"{path.name}: {tok}")
    assert not violations, f"model-name tokens found on the JOAT surface: {violations}"


# ── Custom (user-authored) teams: persistence + merge (DESIGN §6.4 builder) ─────

@pytest.fixture
def teams_dir(tmp_path):
    """Point teams at an isolated on-disk dir for the duration of a test."""
    teams.set_custom_teams_dir(tmp_path)
    yield tmp_path
    teams.set_custom_teams_dir(None)


def _sample_roles() -> dict:
    return {
        "apex": {"backend": "claude_api", "escalation_chain": ["claude_code"]},
        "worker": {"backend": "deepseek_v4_flash", "escalation_chain": ["glm_5_2"]},
        "critic": {"backend": "local"},
    }


def test_create_team_persists_and_lists(teams_dir):
    created = teams.create_team("my-fleet", _sample_roles())
    assert created["name"] == "my-fleet"
    assert created["builtin"] is False
    assert (teams_dir / "my-fleet.yaml").exists()

    assert "my-fleet" in teams.known_teams()
    names = {t["name"] for t in teams.list_teams()}
    assert {"cloud-heavy", "demo-fleet", "local-first", "my-fleet"} <= names
    mine = next(t for t in teams.list_teams() if t["name"] == "my-fleet")
    assert mine["builtin"] is False
    assert mine["roles"]["critic"][0]["backend"] == "local"
    assert mine["roles"]["critic"][0]["escalation_chain"] == []  # normalized to a 1-slot list


@pytest.mark.asyncio
async def test_resolve_uses_a_custom_team(teams_dir):
    teams.create_team("my-fleet", _sample_roles())
    face = _face(preferred_backend="local", escalation_backend="local", role="worker")
    # Default team → passthrough; custom pin → the team's worker backend.
    assert await teams.resolve("worker", face=face) == "local"
    assert await teams.resolve("worker", face=face, team="my-fleet") == "deepseek_v4_flash"
    assert teams.escalation_chain("worker", face=face, team="my-fleet") == ["glm_5_2"]


def test_create_team_rejects_unknown_backend(teams_dir):
    with pytest.raises(ValueError, match="not a known backend"):
        teams.create_team("bad", {"apex": {"backend": "no-such-backend"}})


def test_create_team_rejects_unknown_role(teams_dir):
    with pytest.raises(ValueError, match="unknown role"):
        teams.create_team("bad", {"manager": {"backend": "claude_api"}})


def test_create_team_rejects_builtin_name(teams_dir):
    with pytest.raises(ValueError, match="built-in"):
        teams.create_team("demo-fleet", _sample_roles())


def test_create_team_rejects_bad_slug(teams_dir):
    with pytest.raises(ValueError, match="slug"):
        teams.create_team("My Fleet!", _sample_roles())


def test_create_team_duplicate_needs_overwrite(teams_dir):
    teams.create_team("dup", _sample_roles())
    with pytest.raises(ValueError, match="already exists"):
        teams.create_team("dup", _sample_roles())
    teams.create_team("dup", _sample_roles(), overwrite=True)  # explicit overwrite OK


def test_delete_team_removes_custom_but_not_builtin(teams_dir):
    teams.create_team("ephemeral", _sample_roles())
    assert teams.delete_team("ephemeral") is True
    assert "ephemeral" not in teams.known_teams()
    assert teams.delete_team("ephemeral") is False  # already gone
    with pytest.raises(ValueError, match="built-in"):
        teams.delete_team("demo-fleet")


def test_malformed_team_files_are_skipped(teams_dir):
    (teams_dir / "broken.yaml").write_text("not: [valid, roles", encoding="utf-8")
    (teams_dir / "wrongbackend.yaml").write_text(
        "name: wrongbackend\nroles:\n  apex:\n    backend: nope\n", encoding="utf-8"
    )
    known = teams.known_teams()
    assert "broken" not in known and "wrongbackend" not in known
    assert "demo-fleet" in known  # builtins still resolve


def test_set_active_team_accepts_a_custom_team(teams_dir):
    teams.create_team("active-custom", _sample_roles())
    teams.set_active_team("active-custom")
    assert teams.get_active_team() == "active-custom"


@pytest.mark.asyncio
async def test_create_team_supports_multiple_slots_per_role(teams_dir):
    """A role can bind more than one backend (a roster); resolve routes to the
    PRIMARY slot (per-subtask selection across the roster is a follow-up)."""
    roles = {
        "worker": [
            {"name": "bulk", "backend": "deepseek_v4_flash", "escalation_chain": ["glm_5_2"]},
            {"name": "tool", "backend": "glm_5_2", "escalation_chain": []},
        ],
    }
    created = teams.create_team("roster", roles)
    assert [s["name"] for s in created["roles"]["worker"]] == ["bulk", "tool"]

    listed = next(t for t in teams.list_teams() if t["name"] == "roster")
    assert len(listed["roles"]["worker"]) == 2

    face = _face(preferred_backend="local", role="worker")
    assert await teams.resolve("worker", face=face, team="roster") == "deepseek_v4_flash"
    assert teams.escalation_chain("worker", face=face, team="roster") == ["glm_5_2"]


def test_legacy_single_dict_team_file_still_loads(teams_dir):
    """A custom file authored in the legacy single-dict shape normalizes to a 1-slot
    list — back-compat for any pre-multi-slot teams on disk."""
    (teams_dir / "legacy.yaml").write_text(
        "name: legacy\nroles:\n  worker:\n    backend: local\n", encoding="utf-8"
    )
    assert "legacy" in teams.known_teams()
    listed = next(t for t in teams.list_teams() if t["name"] == "legacy")
    assert listed["roles"]["worker"][0]["backend"] == "local"


# ── Profiles: superset store (role prompts + shape + seats + bounds) ────────────

def test_slots_carry_role_prompt(teams_dir):
    created = teams.create_team("rp-team", {"worker": {"backend": "local", "role_prompt": "be terse"}})
    assert created["roles"]["worker"][0]["role_prompt"] == "be terse"


def test_create_profile_persists_full_envelope(teams_dir):
    env = teams.create_profile("council-fast", {
        "roles": {"apex": {"backend": "claude_api", "role_prompt": "lead"}},
        "seats": [
            {"posture": "framer", "backend": "claude_api", "role_prompt": "frame it"},
            {"posture": "stress", "backend": "gemini_flash"},
        ],
        "shape": "fusion",
        "synth_backend": "minimax",
        "protocol_bounds": {"max_rounds": 2, "max_usd": 1.0, "grounding": "off"},
    })
    assert env["builtin"] is False
    assert env["shape"] == "fusion"
    assert env["roles"]["apex"][0]["role_prompt"] == "lead"
    assert env["seats"][0]["role_prompt"] == "frame it"
    assert env["seats"][1]["role_prompt"] == ""  # normalized empty
    assert (teams_dir / "council-fast.yaml").exists()

    loaded = teams.load_profile("council-fast")
    assert loaded["shape"] == "fusion"
    assert loaded["synth_backend"] == "minimax"
    assert loaded["protocol_bounds"]["grounding"] == "off"
    assert [s["posture"] for s in loaded["seats"]] == ["framer", "stress"]


def test_load_profile_builtin_is_roles_only(teams_dir):
    env = teams.load_profile("demo-fleet")
    assert env["builtin"] is True
    assert "shape" not in env and "seats" not in env
    assert env["roles"]["worker"][0]["backend"] == "deepseek_v4_flash"
    assert env["roles"]["worker"][0]["role_prompt"] == ""  # builtins carry empty prompts


def test_load_profile_plain_team_has_no_shape(teams_dir):
    teams.create_team("plain", {"worker": {"backend": "local"}})
    env = teams.load_profile("plain")
    assert "shape" not in env and "seats" not in env
    assert env["roles"]["worker"][0]["backend"] == "local"


def test_load_profile_unknown_is_none(teams_dir):
    assert teams.load_profile("does-not-exist") is None


def test_validate_profile_rejects_bad_shape_and_seat_backend(teams_dir):
    with pytest.raises(ValueError, match="unknown shape"):
        teams.create_profile("bad1", {"roles": {"worker": {"backend": "local"}}, "shape": "spiral"})
    with pytest.raises(ValueError, match="not a known backend"):
        teams.create_profile("bad2", {"seats": [{"posture": "framer", "backend": "no-such-backend"}]})


def test_create_profile_requires_roles_or_seats(teams_dir):
    with pytest.raises(ValueError, match="roles.*seats"):
        teams.create_profile("empty", {"shape": "fusion"})


def _bounds_profile(**bounds):
    return {"seats": [{"posture": "framer"}], "protocol_bounds": bounds}


def test_validate_profile_rejects_bad_protocol_bounds():
    assert any("max_usd" in e for e in teams.validate_profile(_bounds_profile(max_usd=0)))
    assert any("max_usd" in e for e in teams.validate_profile(_bounds_profile(max_usd=-1)))
    assert any("max_usd" in e for e in teams.validate_profile(_bounds_profile(max_usd="lots")))
    assert any("max_usd" in e for e in teams.validate_profile(_bounds_profile(max_usd=True)))
    assert any("grounding" in e for e in teams.validate_profile(_bounds_profile(grounding="disabledd")))
    assert any("drift_threshold" in e for e in teams.validate_profile(_bounds_profile(drift_threshold=1.5)))
    assert any("restart_budget" in e for e in teams.validate_profile(_bounds_profile(restart_budget=-1)))
    assert any("max_rounds" in e for e in teams.validate_profile(_bounds_profile(max_rounds=0)))
    # Above the recursion-budget ceiling is rejected at author time (not mid-run crash).
    from core.config import COUNCIL_MAX_ROUNDS_CEILING
    assert any("max_rounds" in e for e in teams.validate_profile(
        _bounds_profile(max_rounds=COUNCIL_MAX_ROUNDS_CEILING + 1)))
    assert teams.validate_profile(_bounds_profile(max_rounds=COUNCIL_MAX_ROUNDS_CEILING)) == []


def test_validate_profile_accepts_valid_protocol_bounds():
    # Full valid bounds — incl. a positive SUB-spawn max_usd (0.10), which is allowed:
    # the runtime ceiling notice flags it, and a tiny intentional ceiling is a valid demo.
    assert teams.validate_profile(_bounds_profile(
        max_usd=0.10, grounding="on", restart_budget=0, drift_threshold=0.34, max_rounds=3)) == []
    # bool + numeric grounding are accepted (runtime coerces them).
    assert teams.validate_profile(_bounds_profile(grounding=True)) == []
    assert teams.validate_profile(_bounds_profile(grounding=0)) == []
    # off-tokens are case/space tolerant.
    assert teams.validate_profile(_bounds_profile(grounding="OFF")) == []


def test_create_profile_rejects_non_positive_max_usd(teams_dir):
    with pytest.raises(ValueError, match="max_usd"):
        teams.create_profile("badbounds", {
            "seats": [{"posture": "framer", "backend": "local"}],
            "protocol_bounds": {"max_usd": 0, "grounding": "on"},
        })


def test_list_profiles_includes_builtins_and_customs(teams_dir):
    teams.create_profile("mine", {"roles": {"worker": {"backend": "local"}}, "shape": "sequential"})
    profiles = teams.list_profiles()
    names = {p["name"] for p in profiles}
    assert {"cloud-heavy", "demo-fleet", "local-first", "mine"} <= names
    mine = next(p for p in profiles if p["name"] == "mine")
    assert mine["shape"] == "sequential" and mine["builtin"] is False
