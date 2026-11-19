"""Provisioning contract, hermetic on Windows and Linux; no login or systemctl."""
from contextlib import contextmanager
import importlib.util
import json
import shutil
from pathlib import Path
import sys
import os
import uuid
from types import SimpleNamespace

import pytest

from core import lane_config

RUNTIME = Path(__file__).resolve().parents[2] / "deploy/runtime/lib"


@pytest.fixture(autouse=True)
def mock_redis():
    # Pure provisioning contracts do not import model transports or use Redis.
    yield


@pytest.fixture
def tmp_path(tmp_path_factory):
    if os.name != 'nt':
        return tmp_path_factory.mktemp('runtime-model')
    path = Path(__file__).parents[1] / 'test-db' / ('runtime-model-' + uuid.uuid4().hex)
    path.mkdir(parents=True, mode=0o777)
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from core import worker as boundary
    monkeypatch.setattr(boundary, 'EXECUTION_BOUNDARY', tmp_path / 'absent-execution-boundary')
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'legacy')
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=1, pw_gid=1)))
        monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace())
    for name in ("common", "lane", "model"):
        spec = importlib.util.spec_from_file_location(name, RUNTIME / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    model = sys.modules["model"]
    common = sys.modules["common"]
    @contextmanager
    def unlocked(*args):
        yield True
    monkeypatch.setattr(model, 'model_locked', unlocked)
    monkeypatch.setattr(model, 'signin_locked', unlocked)
    monkeypatch.setattr(model, 'read_signin_auth', lambda root, home:
        json.loads((home / 'auth.json').read_text()) if (home / 'auth.json').exists() else {})
    @contextmanager
    def signin_home(root):
        home = root / 'signin-test'
        home.mkdir(mode=0o777)
        try:
            yield home
        finally:
            shutil.rmtree(home)
    monkeypatch.setattr(model, 'signin_home', signin_home)
    monkeypatch.setattr(model, "FAMILY", tmp_path)
    monkeypatch.setattr(common.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1, pw_gid=1))
    monkeypatch.setattr(common.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setattr(common, "private_write", lambda path, content, *a: path.write_text(content))
    monkeypatch.setattr(model, "private_replace", lambda path, content, *a: path.write_text(content))
    monkeypatch.setattr(lane_config.shutil, "which", lambda name: "/fake/codex")
    root = tmp_path / "new-lane/bob-memory"
    root.mkdir(parents=True)
    if os.name == 'nt':
        # POSIX modes are tested on Linux; preserve the managed runner's DACL.
        (root / 'codex-home').mkdir(mode=0o777)
        monkeypatch.setattr(common.os, 'chmod', lambda *a: None)
    (root / "lane.env").write_text('BOB_DEFAULT_OWNER="new-lane"\n')
    monkeypatch.setattr(model, "visible_openai_models", lambda root, **kwargs: ["account-default", "gpt-6.1-sol", "account-cheaper"])
    model.restarts = []
    monkeypatch.setattr(model, "systemctl", lambda *a: model.restarts.append(a))
    return model, common, root


def test_default_requires_configured_provider(runtime):
    model, _, root = runtime
    assert model.status("new-lane")["default_backend"] is None
    for backend in ("claude", "deepseek_v4_flash", "codex_code", "openai_codex", "minimax"):
        with pytest.raises(ValueError, match="needs provider"):
            model.set_default(root, backend)
    assert model.read_env(root / "lane.env").get("BOB_LANE_DEFAULT_BACKEND") is None


@pytest.mark.parametrize('provider', ['anthropic', 'deepseek', 'google', 'glm', 'kimi', 'qwen'])
def test_each_provider_enrolls_only_its_preset_and_rotation_preserves_choices(runtime, provider):
    model, _, root = runtime
    spec = model.SETUP[provider]
    model.set_key('new-lane', provider, 'fake-first')
    assert set(lane_config.read_lane(root).backends) == {spec['backend']}
    assert model.status('new-lane')['providers'][provider + '_key']
    values = model.read_env(root / 'lane.env')
    values[spec['model_env']] = 'owner-model'
    model.save_env(root / 'lane.env', values)
    model.set_key('new-lane', provider, 'fake-rotated')
    assert model.status('new-lane')['default_backend'] == spec['backend']
    assert lane_config.read_lane(root).backends[spec['backend']].model == 'owner-model'
    assert set(lane_config.read_lane(root).backends) == {spec['backend']}


def test_presets_are_saved_and_second_provider_preserves_default(runtime):
    model, _, root = runtime
    model.set_key("new-lane", "anthropic", "fake-a")
    first = model.read_env(root / "lane.env")
    assert first["BOB_LANE_DEFAULT_BACKEND"] == "claude_api"
    assert first["ANTHROPIC_MODEL"] == model.SETUP["anthropic"]["model"]
    model.set_key("new-lane", "deepseek", "fake-d")
    second = model.read_env(root / "lane.env")
    assert second["BOB_LANE_DEFAULT_BACKEND"] == "claude_api"
    assert second["DEEPSEEK_MODEL"] == model.SETUP["deepseek"]["model"]
    assert model.status("new-lane")["ready"]
    assert set(lane_config.read_lane(root).backends) == {"claude_api", "deepseek_v4_flash"}
    model.set_default(root, "claude")
    assert model.read_env(root / "lane.env")["BOB_LANE_DEFAULT_BACKEND"] == "claude_api"
    model.set_default(root, "deepseek_v4_flash")
    assert model.status("new-lane")["default_backend"] == "deepseek_v4_flash"
    assert len(model.restarts) == 2


async def test_deepseek_reported_name_reaches_gateway_catalog_and_workflow_binding(runtime):
    from core.roster import AgentServices, load_roster
    from core.execution.dispatcher import exact_models
    from core.execution.workflows import WorkflowRuntime
    from unittest.mock import AsyncMock
    model, _, root = runtime
    assert model.SETUP['deepseek']['reported_model_names'] == ['deepseek-flash']
    assert '2026-11-17' in model.SETUP['deepseek']['reported_model_names_comment']
    assert all('reported_model_names' not in spec for key,spec in model.SETUP.items() if key != 'deepseek')
    model.set_key('new-lane', 'deepseek', 'fake-key')
    lane = lane_config.read_lane(root)
    definitions = lane_config.agent_service_definitions([b.row() for b in lane.backends.values()])
    # This is the gateway's strict configured-catalog validation, not an untyped copy.
    validated = AgentServices.model_validate(definitions)
    specs = {key: spec.as_supplied() for key,spec in validated.models.items()}
    assert len(specs) == 1
    assert next(iter(specs.values()))['reported_model_names'] == ['deepseek-flash']
    workflow = WorkflowRuntime(SimpleNamespace(models=AsyncMock(return_value=exact_models(specs))))
    binding = (await workflow.bindings(None))[0]
    assert binding['reported_model_names'] == ['deepseek-flash']
    assert binding['model'] == 'deepseek-v4-flash'
    roster = load_roster(Path(__file__).parent / "fixtures/deployment/roster.yaml")
    assert roster.agent_services.models['bob-ds-flash'].reported_model_names == ['deepseek-flash']
    # The preset does not bless an owner's unrelated model or another transport.
    for backend,requested in [('deepseek_v4_flash','owner-model'),('deepseek_v4','deepseek-v4-flash')]:
        other = lane_config.agent_service_definitions([{'backend':backend,'models':[requested],'available':True}])
        assert 'reported_model_names' not in next(iter(other['models'].values()))


def test_existing_model_and_stale_default_are_not_silently_replaced(runtime):
    model, _, root = runtime
    model.save_env(root / "lane.env", {"ANTHROPIC_MODEL": "user-model"})
    model.set_key("new-lane", "anthropic", "fake-a")
    assert model.read_env(root / "lane.env")["ANTHROPIC_MODEL"] == "user-model"
    values = model.read_env(root / "lane.env")
    values["BOB_LANE_DEFAULT_BACKEND"] = "minimax"
    model.save_env(root / "lane.env", values)
    with pytest.raises(ValueError, match="default setup error.*minimax"):
        model.set_key("new-lane", "deepseek", "fake-d")
    assert model.status("new-lane")["default_backend"] == "minimax"
    assert not model.status("new-lane")["ready"]


def test_new_codex_home_has_no_mini_and_existing_profiles_survive(runtime):
    model, common, root = runtime
    home = common.ensure_codex_home(root)
    text = (home / "config.toml").read_text()
    assert "[profiles.gpt]" in text and "gpt-mini" not in text and "model =" not in text
    existing = text + '\n[profiles.gpt-mini]\nmodel = "existing-user-choice"\n'
    (home / "config.toml").write_text(existing)
    common.ensure_codex_home(root)
    assert (home / "config.toml").read_text() == existing
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "fake-key"}))
    model.save_preset(root, "openai")
    model.set_default(root, "codex_code")
    row = lane_config.read_lane(root).backends["codex_code"].row()
    assert row["model"] is None and row["models"] == []


def test_openai_key_login_saves_only_lane_auth_and_no_model_pin(runtime, monkeypatch):
    model, _, root = runtime
    def login(argv, *, input, **kwargs):
        assert argv == [model.CODEX, "login", "--with-api-key"]
        assert set(kwargs["env"]) == {"PATH", "HOME", "CODEX_HOME"}
        (root / "codex-home/auth.json").write_text(json.dumps({"OPENAI_API_KEY": input}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(model.subprocess, "run", login)
    assert model.set_key("new-lane", "openai", "fake-lane-key")["default_backend"] == "codex_code"
    values = model.read_env(root / "lane.env")
    assert "CODEX_MODEL" not in values
    assert not (root / "providers.env").exists()
    assert model.status("new-lane")["ready"]


@pytest.mark.parametrize("existing", [None, "codex_code", "claude_api"])
@pytest.mark.parametrize("catalog_readable", [True, False])
def test_signin_defaults_to_http_and_preserves_valid_selection(runtime, monkeypatch, existing, catalog_readable):
    model, common, root = runtime
    home = model.prepare(root)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-lane-access"}}))
    if existing == "claude_api":
        model.set_key("new-lane", "anthropic", "fake-key")
    elif existing:
        model.set_default(root, existing)
    if not catalog_readable:
        from core.backends.openai_codex_auth import CodexProviderError
        def unavailable(root, **kwargs): raise CodexProviderError("offline")
        monkeypatch.setattr(model, "visible_openai_models", unavailable)
    # Exercise openai_login's success/default handling with a fake completed CLI.
    class Process:
        stdout = SimpleNamespace()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self, **kwargs): return 0
        def poll(self): return 0
    class Selector:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def register(self, *args): pass
        def get_map(self): return {}
    def login(*args, **kwargs):
        home = Path(kwargs['env']['CODEX_HOME'])
        (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'new-access'}}))
        return Process()
    monkeypatch.setattr(model.subprocess, "Popen", login)
    monkeypatch.setattr(model.selectors, "DefaultSelector", Selector)
    assert model.openai_login("new-lane")
    assert lane_config.read_lane(root).backends["openai_codex"].model == ("account-default" if catalog_readable else None)
    assert model.status("new-lane")["default_backend"] == (existing or "openai_codex")
    assert set(lane_config.read_lane(root).backends) >= {"codex_code", "openai_codex"}
    model.set_default(root, "codex_code")
    assert model.status("new-lane")["default_backend"] == "codex_code"
    model.set_default(root, "openai_codex")
    assert model.status("new-lane")["default_backend"] == "openai_codex"


def test_http_model_key_fallback_and_no_cli_binary_dependency(runtime, monkeypatch):
    model, _, root = runtime
    home = model.prepare(root)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-token"}}))
    monkeypatch.setattr(lane_config.shutil, "which", lambda name: None)
    cfg = lane_config.read_lane(root)
    assert cfg.backends["openai_codex"].available
    assert not cfg.backends["codex_code"].available
    values = model.read_env(root / "lane.env")
    values["CODEX_MODEL"] = "existing-lane-model"
    model.save_env(root / "lane.env", values)
    assert lane_config.read_lane(root).backends["openai_codex"].model == "existing-lane-model"
    values["OPENAI_CODEX_MODEL"] = "http-lane-model"
    model.save_env(root / "lane.env", values)
    cfg = lane_config.read_lane(root)
    assert cfg.backends["openai_codex"].model == "http-lane-model"
    # CC1: with a ChatGPT sign-in the coding agent follows the owner's selected model.
    assert cfg.backends["codex_code"].model == "http-lane-model"
    (home / "config.toml").write_text('[profiles.gpt]\nmodel_provider = "another-provider"\n')
    cfg = lane_config.read_lane(root)
    assert not cfg.backends["codex_code"].available
    assert cfg.backends["openai_codex"].available
