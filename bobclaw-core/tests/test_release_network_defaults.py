"""Release network-containment defaults."""

import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = REPO_ROOT / "bobclaw-core"


def _core_host(host_override=None):
    env = dict(os.environ)
    env.pop("BOBCLAW_CORE_HOST", None)
    if host_override is not None:
        env["BOBCLAW_CORE_HOST"] = host_override
    env["PYTHONPATH"] = str(CORE_DIR)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            # Neutralize dotenv BEFORE importing config so a dev's repo-local
            # .secrets/bobclaw.env or .env cannot inject BOBCLAW_CORE_HOST and mask
            # the true compiled-in fallback default. The real config module is still
            # imported and its os.getenv fallback is exercised.
            "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; "
            "from core.config import config; print(config.HOST)",
        ],
        cwd=CORE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_core_host_defaults_to_loopback():
    assert _core_host() == "127.0.0.1"


def test_core_host_explicit_override_is_respected():
    assert _core_host("0.0.0.0") == "0.0.0.0"


def test_compose_published_ports_bind_loopback_only():
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )

    for service_name, service in compose["services"].items():
        for port in service.get("ports", []):
            if isinstance(port, str):
                assert port.startswith("127.0.0.1:"), (
                    service_name,
                    port,
                )
            else:
                assert isinstance(port, dict), (service_name, port)
                assert port.get("host_ip") == "127.0.0.1", (
                    service_name,
                    port,
                )


def test_compose_postgres_password_fails_closed_without_weak_default():
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    password = compose["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]

    assert "${POSTGRES_PASSWORD" in password
    assert ":?" in password
    assert password != "bobclaw"
