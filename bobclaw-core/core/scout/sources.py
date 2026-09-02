"""Scout sources v0 (module M1 / MS8-SC1).

Scout does NOT fork its own sources schema — it **rides the Watch pipe substrate
(S1)**: the scout ``sources_v0.yaml`` is a ``core.watch.registry.SourcesRegistry``
(read-only import; inv. 12 — no edits to ``core/watch/``). This module is the thin
locator/loader that points the shared registry loader at Scout's own, fuller source
list (the complete fleet BoB routes + model registries + local-runtime + MCP
directories + community/benchmark channels per MODULES.md M1).

Fixture/offline only in v0 (inv. 11): the URLs are never fetched by the M-lane; the
first live sweep is a post-merge attended smoke. No credentials in v0 — every source
is public (``auth_required: false``, enforced by the shared registry validator).
"""
from __future__ import annotations

from pathlib import Path

from core.watch.registry import SourcesRegistry, load_sources

_SCOUT_SOURCES_PATH = Path(__file__).parent / "sources_v0.yaml"


def scout_sources_path() -> Path:
    """Return the path to Scout's own sources YAML file."""
    return _SCOUT_SOURCES_PATH


def load_scout_sources(path: Path | None = None) -> SourcesRegistry:
    """Load and validate Scout's sources registry (defaults to ``sources_v0.yaml``).

    Reuses the Watch-pipe registry loader/validator verbatim (S1 substrate).
    """
    return load_sources(path if path is not None else _SCOUT_SOURCES_PATH)
