"""BoBClaw TUI — shared test helpers.

The 0600 file-mode discipline (token cache, state file) is enforced via
``os.open``/``os.fchmod`` in product code, but Windows cannot express Unix
mode bits: ``os.chmod`` there only toggles the read-only flag and a normal
writable file reads back as ``0o666``. So the mode assertions are asserted
only where the OS can represent them.
"""
from __future__ import annotations

import stat
import sys

POSIX_MODE_ASSERT = not sys.platform.startswith("win")


def assert_private_mode(path) -> None:
    """Assert ``path`` has mode 0600 — POSIX only (no-op on Windows)."""
    if POSIX_MODE_ASSERT:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)
