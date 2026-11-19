"""Existing gateway tests exercise the explicit deployment fixture."""
import os
from pathlib import Path
os.environ.setdefault("BOB_ROSTER_FILE", str(Path(__file__).resolve().parents[2] / "bobclaw-core/tests/fixtures/deployment/roster.yaml"))

# Explicit synthetic deployment values for legacy regression contracts.
os.environ.setdefault("BOB_FAMILY_ROOT", "/var/lib/example/family")
os.environ.setdefault("BOB_DEFAULT_OWNER", "dad")
os.environ.setdefault("BOB_MEMORY_OWNERS", "dad,luna,sam,alex,shared")
os.environ.setdefault("BOB_SPS_LANE_ROOT", "/var/lib/example/work")
