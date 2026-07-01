"""Real :class:`~core.gui.surface.Surface` adapters (display-bound, behind the ABC).

MS2-G5 ships the **CDP/browser** adapter first (DESIGN-MS-D1 §3-G5 / OD#5 — Windows
native Computer-Use pipes are upstream-broken per ``[[codex-cua-windows-gotchas]]``; the
browser/CDP control path is the reliable one). The pyautogui/UIA **desktop** adapter is a
higher-risk later sprint (deliberately deferred). These adapters do real I/O, so they live
behind the ABC; the loop + the deterministic skeleton tests keep using ``FakeSurface``.
"""
from __future__ import annotations

from core.gui.surfaces.cdp import CdpError, CdpSurface

__all__ = ["CdpSurface", "CdpError"]
