"""The pre-extraction oracle is immutable; run each comparison in a fresh process."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from core.roster import (DeploymentRoster,
                         RosterConfigurationError, get_roster, load_roster)

ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROSTER_FILE = ROOT / "bobclaw-core/tests/fixtures/deployment/roster.yaml"
GOLDENS = Path(__file__).parent / "roster_golden"
CAPTURE = GOLDENS / "capture.py"


def run_capture(surface="core", selector=None, *flags):
    env = dict(os.environ)
    env.pop("BOB_ROSTER_FILE", None)
    if selector is not None:
        env["BOB_ROSTER_FILE"] = str(selector)
    return subprocess.run([sys.executable, "-X", "utf8", str(CAPTURE), surface, *flags],
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          timeout=60, cwd=ROOT)


@pytest.mark.parametrize("explicit", [True], ids=["explicit-deployment"])
def test_core_golden_and_single_factory(explicit):
    result = run_capture("core", LEGACY_ROSTER_FILE if explicit else None,
                         "--check-registry-identity")
    assert result.returncode == 0, result.stderr
    expected = (GOLDENS / "core.json").read_text(encoding="utf-8")
    assert result.stdout == expected


@pytest.mark.parametrize("surface", ["core", "gateway"])
def test_golden_bytes_match_pre_refactor_provenance(surface):
    provenance = json.loads((GOLDENS / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["tracked_diff"] == ""
    assert hashlib.sha256((GOLDENS / f"{surface}.json").read_bytes()).hexdigest() == \
        provenance["golden_sha256"][surface]


def test_manifest_schema_and_relative_profiles():
    roster = load_roster(LEGACY_ROSTER_FILE)
    assert roster.schema_version == 1
    assert roster.profiles_path == LEGACY_ROSTER_FILE.parent / "faces"
    assert len(list(roster.profiles_path.glob("*.yaml"))) == 33
    assert DeploymentRoster.model_json_schema()["additionalProperties"] is False


@pytest.fixture
def data():
    value = yaml.safe_load(LEGACY_ROSTER_FILE.read_text(encoding="utf-8"))
    value["profiles_dir"] = str(LEGACY_ROSTER_FILE.parent / "faces")
    return value


@pytest.mark.parametrize("mutation", [
    "version", "boolean-version", "unknown-field", "missing-field", "bad-bool",
    "unknown-seat", "unknown-worker", "unknown-council", "unknown-face",
    "bad-role", "missing-profiles", "bad-fallback", "missing-fallback",
])
def test_schema_rejects_invalid_policy(tmp_path, data, mutation):
    if mutation == "version": data["schema_version"] = 2
    elif mutation == "boolean-version": data["schema_version"] = True
    elif mutation == "unknown-field": data["unrecognized"] = "no"
    elif mutation == "missing-field": del data["routing"]
    elif mutation == "bad-bool": data["agent_services"]["models"]["bob-glm-53"]["enabled"] = "true"
    elif mutation == "unknown-seat": data["agent_services"]["teams"]["coding"]["manager"] = "missing"
    elif mutation == "unknown-worker": data["agent_services"]["worker_choices"]["balance-budget"] = ["missing"]
    elif mutation == "unknown-council": data["defaults"]["council_default_seats"] = ["missing"]
    elif mutation == "unknown-face": data["routing"]["intent_faces"]["worker"] = "missing"
    elif mutation == "bad-role": data["builtin_teams"]["cloud-heavy"]["invalid"] = {"backend": "local", "escalation_chain": []}
    elif mutation == "missing-profiles": data["profiles_dir"] = "missing"
    elif mutation == "bad-fallback": data["defaults"]["execute_fallbacks"]["pi_error"] = 17
    elif mutation == "missing-fallback": del data["defaults"]["execute_fallbacks"]["pi_error"]
    path = tmp_path / "roster.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RosterConfigurationError, match="BOB_ROSTER_FILE configuration error"):
        load_roster(path)


@pytest.mark.parametrize("content", ["[bad: yaml", "{}", "null", "schema_version: 1\nschema_version: 1\n"])
def test_malformed_explicit_selector_never_falls_back(tmp_path, monkeypatch, content):
    path = tmp_path / "broken.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("BOB_ROSTER_FILE", str(path))
    with pytest.raises(RosterConfigurationError, match="BOB_ROSTER_FILE configuration error"):
        get_roster()


@pytest.mark.parametrize("selector", ["", "does-not-exist.yaml"])
def test_missing_or_empty_explicit_selector_never_falls_back(monkeypatch, selector):
    monkeypatch.setenv("BOB_ROSTER_FILE", selector)
    with pytest.raises(RosterConfigurationError, match="BOB_ROSTER_FILE configuration error"):
        get_roster()


@pytest.mark.parametrize("missing", [False, True], ids=["malformed", "missing"])
def test_core_startup_fails_closed(tmp_path, missing):
    path = tmp_path / "broken.yaml"
    if not missing:
        path.write_text("schema_version: 99\n", encoding="utf-8")
    result = run_capture("core", path)
    assert result.returncode != 0
    assert "BOB_ROSTER_FILE configuration error" in result.stderr
    assert not result.stdout


def test_runtime_archive_allowlist_excludes_rosters():
    script = (ROOT / "deploy/runtime/build.sh").read_text(encoding="utf-8")
    archive = script.split('archive --format=tar HEAD --', 1)[1].split('| tar', 1)[0]
    # Match the actual archive operands; comments elsewhere are irrelevant.
    operands = re.findall(r"'[^']*'|[^\s\\]+", archive)
    included = [p.strip("'") for p in operands if not p.startswith("':(exclude)")]
    assert included
    assert not any("rosters" in p.split("/") for p in included)
    assert all(p != "." and not any(c in p for c in "*?[") for p in included)


def test_registry_switches_selector_without_reusing_previous_profiles(tmp_path, data, monkeypatch):
    from core.faces.registry import get_registry, get_default_registry
    path = tmp_path / "roster.yaml"
    data = deepcopy(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.delenv("BOB_ROSTER_FILE", raising=False)
    legacy = get_registry()
    monkeypatch.setenv("BOB_ROSTER_FILE", str(path))
    selected = get_registry()
    assert selected is get_default_registry() is get_registry()
    assert selected is not legacy
    monkeypatch.setenv("BOB_ROSTER_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(RosterConfigurationError):
        get_registry()


def test_environment_overrides_keep_existing_precedence():
    overrides = {
        "GATE_CRITIC_BACKEND": "glm_5_3",
        "CRITIC_FALLBACK_BACKEND": "",
        "COUNCIL_GROUND_BACKEND": "fixture-ground",
        "COUNCIL_SEAT_BACKENDS": json.dumps({"stress": {"backend": "minimax"}}),
    }
    result = run_capture("core", LEGACY_ROSTER_FILE, "--env-json", json.dumps(overrides))
    assert result.returncode == 0, result.stderr
    council = json.loads(result.stdout)["council"]
    original = json.loads((GOLDENS / "core.json").read_text(encoding="utf-8"))["council"]
    assert council["gate_critic"] == "glm_5_3"
    assert council["fallback_critic"] == ""
    assert council["ground_backend"] == "fixture-ground"
    assert council["backends"]["stress"]["backend"] == "minimax"
    assert council["backends"]["stress"]["fallback_chain"] == original["backends"]["stress"]["fallback_chain"]
    assert council["backends"]["framer"] == original["backends"]["framer"]


def test_changed_manifest_controls_live_selection(tmp_path, data):
    data["routing"]["intent_faces"]["worker"] = "researcher"
    data["routing"]["class_faces"]["worker"] = "reviewer"
    data["defaults"]["execute_fallbacks"] = {
        key: "glm_5_3" for key in data["defaults"]["execute_fallbacks"]
    }
    data["defaults"]["council_seat_backends"]["framer"]["backend"] = "minimax"
    data["builtin_teams"]["cloud-heavy"]["worker"]["backend"] = "minimax"
    path = tmp_path / "roster.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = run_capture("core", path)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["intent_faces"][0][0] == "researcher"
    assert out["class_faces"]["worker"] == "reviewer"
    for calls in out["execute_fallbacks"].values():
        assert calls["implicit"][-1] == "glm_5_3"
        assert calls["explicit"][-1] == "deepseek_v4_flash"
    assert out["council"]["backends"]["framer"]["backend"] == "minimax"
    team = next(team for team in out["teams"] if team["name"] == "cloud-heavy")
    assert team["roles"]["worker"][0]["backend"] == "minimax"


def test_generic_example_is_valid_and_unset_never_selects_legacy(monkeypatch):
    from core.roster import DEFAULT_ROSTER_FILE
    roster = load_roster(DEFAULT_ROSTER_FILE)
    assert roster.agent_services.models == {}
    assert roster.agent_services.teams == {}
    assert roster.profiles_path == ROOT / "bobclaw-core/core/faces/templates"
    monkeypatch.delenv("BOB_ROSTER_FILE", raising=False)
    assert get_roster().file is None
    assert get_roster().agent_services.models == {}
