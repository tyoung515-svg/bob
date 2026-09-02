"""BoBClaw TUI — Textual theme + glyph system (MS6-T2 / T5).

Two responsibilities, both about *presentation only* (no data-plane behaviour):

  1. **Theme.** A BoBClaw Textual theme in a **dark** and a **light** variant, mapping the
     shared UI vocabulary (``tasks/2026-07-07-fable-uiux/VOCABULARY.md`` §1
     ``success | warn | alert``) onto Textual's ``$success | $warning | $error`` theme
     variables. Widgets reference the *variables*, never raw hex (VOCABULARY §1 rule
     "no raw hex in widgets") — the only hex in the whole cockpit lives HERE, in the two
     palette definitions. ``BOBCLAW_TUI_THEME`` pins the initial variant; ``/theme`` (and
     the ``t`` binding) toggle it live.

  2. **Glyphs.** The VOCABULARY §4 glyph→ASCII fallback table, one source of truth for the
     ``/ascii`` toggle so nothing in the cockpit depends on a Nerd-font / emoji:
     ``🛰→* · ✓→ok · ↝→-> · 🔧→[t] · ●→o · …→...``.

This module imports ``textual.theme`` (only used when the cockpit actually runs), so it is
imported inside ``app.py``'s Textual guard — the pure data layer never needs it.
"""
from __future__ import annotations

import os

from textual.theme import Theme

# ── themes ──────────────────────────────────────────────────────────────────
# Dark values track the app's landed gui-theme (GitHub-dark-ish canvas #0F1316), tuned
# for ≥4.5:1 text contrast. Light values are the VOCABULARY §1 / UIUX-PLAN §4.1 proposals
# (success #1F883D · warn #B58500 · alert #D9530B on a #F6F8F9 canvas) — beta-flagged there,
# a §6.3 Travis sign-off for the app, but fine to render in the watch-only TUI.
BOBCLAW_DARK = Theme(
    name="bobclaw-dark",
    primary="#58A6FF",
    secondary="#8B949E",
    accent="#58A6FF",
    foreground="#E6EDF3",
    background="#0F1316",
    surface="#161B22",
    panel="#1C2128",
    success="#3FB950",   # VOCABULARY success — worker ok / live monitor / verified
    warning="#D29922",   # VOCABULARY warn    — reconnecting / reroute / dropped-seat
    error="#F85149",     # VOCABULARY alert   — DOWN:redis / correction / rejected
    dark=True,
)

BOBCLAW_LIGHT = Theme(
    name="bobclaw-light",
    primary="#0969DA",
    secondary="#57606A",
    accent="#0969DA",
    foreground="#16212A",
    background="#F6F8F9",
    surface="#FFFFFF",
    panel="#EFF2F4",
    success="#1F883D",   # VOCABULARY §1 light success (≥4.5:1 on white)
    warning="#B58500",   # VOCABULARY §1 light warn
    error="#D9530B",     # VOCABULARY §1 light alert
    dark=False,
)

BOBCLAW_THEMES = (BOBCLAW_DARK, BOBCLAW_LIGHT)
DARK_NAME = BOBCLAW_DARK.name
LIGHT_NAME = BOBCLAW_LIGHT.name


def resolve_initial_theme(env: dict | None = None) -> str:
    """Pick the startup theme name from ``BOBCLAW_TUI_THEME`` (``dark`` | ``light`` | a full
    theme name), defaulting to the dark variant. Unknown values fall back to dark rather
    than crash — a pinned-theme typo must never stop the cockpit from starting."""
    env = env if env is not None else os.environ
    raw = (env.get("BOBCLAW_TUI_THEME") or "").strip().lower()
    if raw in ("light", LIGHT_NAME):
        return LIGHT_NAME
    if raw in ("dark", DARK_NAME):
        return DARK_NAME
    return DARK_NAME


def next_theme(current: str) -> str:
    """Toggle dark ⇄ light (``/theme`` / the ``t`` binding). Any non-light current (incl. a
    Textual built-in the user somehow set) toggles to light, so the pair is a clean flip."""
    return DARK_NAME if current == LIGHT_NAME else LIGHT_NAME


# ── glyphs (VOCABULARY §4 table — the /ascii source of truth) ────────────────
# name -> (emoji/glyph, ascii-fallback). The ASCII form is the *canonical semantic*
# (VOCABULARY §4 "screen reader / non-emoji terminal loses nothing"); the glyph is a skin.
_GLYPHS: dict[str, tuple[str, str]] = {
    "sat":      ("🛰", "*"),     # named-flight marker (vs · ambient)
    "ok":       ("✓", "ok"),    # resolved / clean (empty approvals inbox, done flight)
    "reroute":  ("↝", "->"),    # rerouted / fallback taken
    "tool":     ("🔧", "[t]"),   # tool-capable face
    "dot":      ("●", "o"),     # health / status dot
    "ellipsis": ("…", "..."),   # truncation marker (… +N more)
}


def glyph(name: str, ascii_mode: bool) -> str:
    """Resolve a VOCABULARY glyph by name for the current glyph mode. Used by the app-layer
    renderers (the flights table markers); the pure ``panels`` formatters carry their own
    ``ascii_mode`` branch so the data layer keeps zero new imports."""
    emoji, fallback = _GLYPHS[name]
    return fallback if ascii_mode else emoji


def resolve_initial_ascii(env: dict | None = None) -> bool:
    """Pick the startup glyph mode from ``BOBCLAW_TUI_ASCII`` (truthy → ASCII), default emoji.
    Symmetric with the theme pin so a font-less terminal can start straight into ASCII."""
    env = env if env is not None else os.environ
    return (env.get("BOBCLAW_TUI_ASCII") or "").strip().lower() in ("1", "true", "yes", "on")
