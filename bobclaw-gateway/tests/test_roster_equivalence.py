"""Gateway oracle uses the same offline capture and pre-refactor evidence as core."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GOLDENS = ROOT / "bobclaw-core/tests/roster_golden"
MANIFEST = ROOT / "bobclaw-core/tests/fixtures/deployment/roster.yaml"


def capture(selector):
    env = dict(os.environ)
    env.pop("BOB_ROSTER_FILE", None)
    if selector is not None:
        env["BOB_ROSTER_FILE"] = str(selector)
    return subprocess.run([
        sys.executable, "-X", "utf8", str(GOLDENS / "capture.py"), "gateway",
    ], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.mark.parametrize("explicit", [True], ids=["explicit-deployment"])
def test_gateway_catalog_and_models_match_golden(explicit):
    result = capture(MANIFEST if explicit else None)
    assert result.returncode == 0, result.stderr
    expected = json.loads((GOLDENS / "gateway.json").read_text(encoding="utf-8"))
    assert json.loads(result.stdout) == apply_roster_changes(expected, "gateway")


def apply_roster_changes(golden, surface):
    """Apply deliberate post-capture roster changes (changes.json) to a golden."""
    changes = json.loads((GOLDENS / "changes.json").read_text(encoding="utf-8"))["changes"]
    for change in (c for c in changes if c["surface"] == surface):
        hits = []

        def walk(node):
            if isinstance(node, dict):
                if all(node.get(k) == v for k, v in change["match"].items()):
                    hits.append(node["bob"] if isinstance(node.get("bob"), dict) else node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(golden)
        assert hits, f"roster change matched nothing: {change}"
        for target in hits:
            target.update(change["set"])
    return golden


@pytest.mark.parametrize("missing", [False, True], ids=["malformed", "missing"])
def test_gateway_startup_fails_closed(tmp_path, missing):
    path = tmp_path / "broken.yaml"
    if not missing:
        path.write_text("schema_version: 99\n", encoding="utf-8")
    result = capture(path)
    assert result.returncode != 0
    assert "BOB_ROSTER_FILE configuration error" in result.stderr
    assert not result.stdout


def test_changed_manifest_controls_service_catalog(tmp_path):
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    data["profiles_dir"] = str(MANIFEST.parent / "faces")
    data["agent_services"]["models"]["bob-glm-53"]["label"] = "Fixture manager"
    data["agent_services"]["teams"]["balance-budget"]["critic"] = "bob-ds-flash"
    data["agent_services"]["worker_choices"]["balance-budget"] = ["bob-ds-flash"]
    path = tmp_path / "roster.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = capture(path)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    catalog = out["/agent-services/catalog"]
    manager = next(m for m in catalog["model_faces"] if m["id"] == "bob-glm-53")
    assert manager["label"] == "Fixture manager"
    team = next(t for t in catalog["teams"] if t["id"] == "balance-budget")
    assert team["critic"] == "bob-ds-flash"
    assert catalog["worker_choices"]["balance-budget"] == ["bob-ds-flash"]
    manager = next(m for m in out["/v1/models"]["data"] if m["id"] == "bob-glm-53")
    assert manager["bob"]["label"] == "Fixture manager"
