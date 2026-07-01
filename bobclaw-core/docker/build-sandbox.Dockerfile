# BoBClaw build-pipeline sandbox image (Feature 2, P3.5).
#
# The verify gate runs LLM-WRITTEN code (pytest imports the generated functions and
# calls them; the CLI runs them). This image is the isolation boundary: at RUN time the
# orchestrator mounts ONLY the per-turn workspace (no host secrets/repo), passes
# --network none, caps memory/pids/cpu, and uses --rm so the container is ephemeral.
# A gate-slipping impl is therefore confined to a throwaway container with no host
# secrets and no network — it cannot read .secrets/bobclaw.env or exfiltrate.
#
# Build once (from bobclaw-core/):
#   docker build -t bobclaw-build-sandbox:py313 -f docker/build-sandbox.Dockerfile docker
#
# Python 3.13 matches the host venv. Only pytest is needed beyond stdlib — the built
# "minikit" app is stdlib-only by contract, so no other deps are installed.
FROM python:3.13-slim

RUN pip install --no-cache-dir pytest

WORKDIR /work
