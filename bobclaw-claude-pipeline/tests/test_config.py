import os
import subprocess
import sys
from pathlib import Path


_PIPELINE_DIR = Path(__file__).resolve().parents[1]


def _config_host(env: dict[str, str]) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            # Neutralize dotenv BEFORE importing config so a dev's repo-local
            # .secrets/bobclaw.env or .env cannot inject HOST and mask the true
            # compiled-in fallback default. The real config module is still imported.
            "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; "
            "import config; print(config.HOST)",
        ],
        cwd=str(_PIPELINE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_host_default_is_loopback() -> None:
    env = dict(os.environ)
    env.pop("HOST", None)

    assert _config_host(env) == "127.0.0.1"


def test_host_env_override_wins() -> None:
    env = dict(os.environ)
    env["HOST"] = "0.0.0.0"

    assert _config_host(env) == "0.0.0.0"
