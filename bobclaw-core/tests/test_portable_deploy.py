"""Deployment artifacts must render safely and create private, usable secrets."""
import base64
import importlib.util
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

from dotenv import dotenv_values
import pytest

REPO = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "deploy" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_env_private_fresh_and_no_overwrite(tmp_path):
    script = load_script("new-env")
    first, second = tmp_path / "first.env", tmp_path / "second.env"
    script.new_env(first)
    old_umask = os.umask(0o777)
    try:
        script.new_env(second)
    finally:
        os.umask(old_umask)
    a, b = dotenv_values(first), dotenv_values(second)
    assert stat.S_IMODE(first.stat().st_mode) == stat.S_IMODE(second.stat().st_mode) == 0o600
    for key in ("BOBCLAW_SECRET", "POSTGRES_PASSWORD", "TOTP_SECRET"):
        assert len(a[key]) >= 32 and a[key] != b[key]
    assert len(base64.b32decode(a["TOTP_SECRET"])) == 20
    assert urlsplit(a["POSTGRES_URL"]).password == a["POSTGRES_PASSWORD"]
    assert all(value == "" for key, value in a.items() if key.endswith("_API_KEY"))
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        script.new_env(first)
    assert first.read_bytes() == before
    link = tmp_path / "link.env"
    link.symlink_to(first)
    with pytest.raises(FileExistsError):
        script.new_env(link)
    assert first.read_bytes() == before


def test_render_units_without_installing(tmp_path):
    script = load_script("render-units")
    home = tmp_path / "Bob home"
    env_file = tmp_path / "settings.env"
    output = tmp_path / "units"
    paths = script.render(home, env_file, output)
    assert {p.name for p in paths} == {"bobclaw-" + name + ".service" for name in ("core", "gateway", "bridge")}
    for path in paths:
        text = path.read_text()
        assert "@BOB_" not in text
        assert f'EnvironmentFile="{env_file}"' in text
        assert f'ExecStart="{home}/.venv/bin/python"' in text
        assert f'Environment="PYTHONPATH={home}/bobclaw-core"' in text
    assert 'start.py' in (output / 'bobclaw-core.service').read_text()
    assert 'tools/bob_bridge.py' in (output / 'bobclaw-bridge.service').read_text()
    assert not env_file.exists()


def test_unit_path_escaping_and_control_refusal(tmp_path):
    script = load_script("render-units")
    home = tmp_path / 'Bob %h $HOME "test"'
    paths = script.render(home, tmp_path / "env", tmp_path / "units")
    core = next(p for p in paths if p.name == "bobclaw-core.service").read_text()
    assert 'Bob %%h $$HOME \\"test\\"' in core
    assert 'Bob %%h $HOME \\"test\\"' in core
    with pytest.raises(ValueError):
        script.render(tmp_path / "bad\npath", tmp_path / "env", tmp_path / "bad-units")
    assert not (tmp_path / "bad-units").exists()
