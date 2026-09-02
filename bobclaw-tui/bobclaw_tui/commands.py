"""BoBClaw TUI — slash-command palette + dispatch (MS6-T2 / T1 split, U4 palette).

Owns the ``/`` command palette: the static command set, the ``CommandInput`` widget that
drives the pop-up menu (like other TUIs), the dispatch that runs a submitted command, and
the ``CapabilitiesClient`` live-registry augmentation (Wave 1B Task 3).

Palette sources: the curated :data:`SLASH_COMMANDS` list is ALWAYS the command source.
The capabilities seam only completes ARGUMENTS — face ids for ``/face``, profile names
for ``/profile``, model+backend ids for ``/model`` — from the gateway's live
``GET /capabilities`` + ``GET /profiles`` (fetched once per session, lazily on the first
palette open). ``/bot`` slugs complete separately from the polled Bots-pane roster
(:func:`bot_arg_completions`). Registry actions are deliberately NOT mapped to commands.
"""
from __future__ import annotations

import logging

from textual.widgets import Input, Log, OptionList

logger = logging.getLogger(__name__)

from bobclaw_tui.panels import cost_line, fmt_tokens

# The slash-command palette. (name, help) — filtered by prefix as you type ``/``.
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "list these commands in the log"),
    ("/chats", "pick a conversation (resume + history)"),
    ("/bots", "focus the Bots pane (teammate roster)"),
    ("/bot", "open a teammate's chat — /bot <slug> [message]"),
    ("/new", "start a new conversation — /new [title]"),
    ("/rename", "rename the active conversation — /rename <title>"),
    ("/profile", "pin a profile on this conversation — /profile <name>"),
    ("/face", "pin a face on this conversation — /face <id>"),
    ("/model", "pin a model (+ optional backend) — /model <model> [backend]"),
    ("/council", "convene council-max for one turn — /council <prompt>"),
    ("/stop", "stop the streaming turn (queued input stays queued)"),
    ("/flight", "filter the monitor to one flight — /flight <id> (empty = all)"),
    ("/theme", "toggle the dark/light cockpit theme"),
    ("/ascii", "toggle emoji ⇄ ASCII glyphs (no font needed)"),
    ("/cost", "show the honest EST cost/token line in the log"),
    ("/refresh", "refresh routing + approvals + flights now"),
    ("/prune", "drop finished (quiescent) flights from the view"),
    ("/clear", "clear the agent/council log"),
]
_MENU_KEYS = {"up", "down", "tab", "enter", "escape"}
# Commands that take NO argument — Enter on an exact match RUNS them (like other TUIs)
# rather than completing to a trailing space. Arg-taking commands always complete.
_ARGLESS = {"/help", "/clear", "/refresh", "/prune", "/theme", "/ascii", "/cost", "/chats",
            "/bots", "/stop"}


# ── live registry augmentation (Wave 1B Task 3) ──
# Gateway response shapes:
#   GET /capabilities (bobclaw-gateway/routers/capabilities.py) →
#     {"faces": [{"id", "display_name", "blurb", ...}],
#      "backends": [{"backend", "model", "available", ...}], "actions": [...], ...}
#   GET /profiles (bobclaw-gateway/routers/teams.py → core /api/profiles) →
#     {"items": [{"name", "builtin", "roles", ...}]}


def parse_face_options(doc) -> list[tuple[str, str]]:
    """``(face_id, description)`` pairs from a ``/capabilities`` document. Null-safe:
    non-dict docs/entries and id-less faces degrade away."""
    faces = doc.get("faces") if isinstance(doc, dict) else None
    out = []
    for f in faces or []:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        out.append((str(f["id"]), str(f.get("display_name") or f.get("blurb") or "")))
    return out


def parse_profile_options(doc) -> list[tuple[str, str]]:
    """``(profile_name, "built-in"|"custom")`` pairs from a ``/profiles`` document."""
    items = doc.get("items") if isinstance(doc, dict) else None
    out = []
    for p in items or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        out.append((str(p["name"]), "built-in" if p.get("builtin") else "custom"))
    return out


def parse_model_options(doc) -> list[tuple[str, str]]:
    """``("model backend", description)`` pairs from the merged backends list of a
    ``/capabilities`` document — ``/model <model> <backend>`` completes both tokens at
    once. Backends with no live model id are skipped."""
    backends = doc.get("backends") if isinstance(doc, dict) else None
    out = []
    for b in backends or []:
        if not isinstance(b, dict) or not b.get("model") or not b.get("backend"):
            continue
        desc = str(b["backend"]) + ("" if b.get("available") else " (unavailable)")
        out.append((f"{b['model']} {b['backend']}", desc))
    return out


_ARG_COMPLETABLE = ("/face", "/profile", "/model")


def bot_arg_completions(items, value: str) -> list[tuple[str, str]]:
    """``/bot`` slug completion from the polled Bots-pane roster (``app._bot_items`` —
    Wave 2 Task 4). Same row shape as :meth:`CapabilitiesClient.completions`: the
    option id is the FULL line, so accepting it fills the input. Empty past the slug
    token (``/bot <slug> <message>`` — the message is free text)."""
    if not value.startswith("/bot "):
        return []
    raw = value[len("/bot "):]
    # Trailing space (or an inner one) means the slug is complete and the
    # free-text message has begun — stop offering slugs (audit P2: stripping
    # before the check hides the trailing space).
    if raw != raw.rstrip() or " " in raw.strip():
        return []
    prefix = raw.strip()
    rows = []
    for b in items or []:
        if not isinstance(b, dict):
            continue
        slug = str(b.get("slug") or "")
        if slug and slug.startswith(prefix):
            rows.append((f"/bot {slug}", f"{slug:<24} {b.get('display_name') or ''}".rstrip()))
    return rows


class CapabilitiesClient:
    """Live palette augmentation over the gateway's registry read surfaces (Wave 1B Task 3).

    The endpoints exist (post-1A): ``GET /capabilities`` + ``GET /profiles``, JWT-gated
    with the same Bearer token the cockpit uses. :meth:`fetch` runs ONCE per session
    (lazily, on the first palette open) and caches three option lists. The curated
    :data:`SLASH_COMMANDS` list stays the command source — this client only completes
    ARGUMENTS for ``/face`` / ``/profile`` / ``/model``; registry actions are NOT mapped
    to commands. Any failure (unreachable gateway, non-200, bad JSON) degrades SILENTLY:
    :attr:`available` stays False and the palette keeps the static list — no error spam.
    """

    def __init__(self, gateway: str, token: str, session) -> None:
        self._base = gateway if "://" in gateway else f"http://{gateway}"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session
        self._fetched = False
        self._ok = False
        self._options: dict[str, list[tuple[str, str]]] = {c: [] for c in _ARG_COMPLETABLE}

    @property
    def available(self) -> bool:
        """Whether the live registry answered — False ⇒ static palette only."""
        return self._ok

    async def fetch(self) -> None:
        """Pull both registries once per session and cache the completion option lists.

        ``_fetched`` latches ONLY on success (audit 1B task-3): a transient failure
        (gateway restart, timeout) must not permanently disable live completions —
        the next palette open retries. Non-200s log the status at debug level (never
        the token) so a 401 is distinguishable from a dead gateway."""
        if self._fetched:
            return
        try:
            async with self._session.get(
                f"{self._base}/capabilities", headers=self._headers
            ) as r:
                if r.status != 200:
                    logger.debug("capabilities fetch: /capabilities -> HTTP %s", r.status)
                    return
                caps = await r.json(content_type=None)
            async with self._session.get(
                f"{self._base}/profiles", headers=self._headers
            ) as r:
                if r.status != 200:
                    logger.debug("capabilities fetch: /profiles -> HTTP %s", r.status)
                    return
                profiles = await r.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 — unreachable gateway ⇒ silent static-only palette
            logger.debug("capabilities fetch failed: %s", type(exc).__name__)
            return
        self._options["/face"] = parse_face_options(caps)
        self._options["/profile"] = parse_profile_options(profiles)
        self._options["/model"] = parse_model_options(caps)
        self._ok = True
        self._fetched = True

    def completions(self, value: str) -> list[tuple[str, str]]:
        """``(option_id, label)`` rows for the ``#cmdmenu`` popup while an arg-taking pin
        command is being typed (``/face <prefix>`` …). The option id is the FULL line, so
        accepting it fills the input. Empty when the registry is unavailable or the cursor
        is past the first argument."""
        if not self._ok:
            return []
        for cmd in _ARG_COMPLETABLE:
            if value.startswith(cmd + " "):
                prefix = value[len(cmd) + 1:].strip()
                if " " in prefix:
                    return []  # past the first argument — nothing to complete
                rows = []
                for arg, desc in self._options[cmd]:
                    token = arg.split(" ", 1)[0]  # /model options carry "model backend"
                    if token.startswith(prefix):
                        rows.append((f"{cmd} {arg}", f"{token:<24} {desc}".rstrip()))
                return rows
        return []

    def commands(self) -> list[tuple[str, str]]:
        """The palette command set: ALWAYS the curated static list — dynamic augmentation
        only adds argument completions (:meth:`completions`), never new commands."""
        return list(SLASH_COMMANDS)


class CommandInput(Input):  # type: ignore[misc]
    """Agent input that drives the slash-command popup like other TUIs.

    While the ``#cmdmenu`` OptionList is open (typing the command token, or an argument
    for ``/face`` / ``/profile`` / ``/model`` — Wave 1B Task 3), ↑/↓ move the highlight,
    Tab/Enter accept the highlighted option into the line, and Esc dismisses it — so
    Enter does NOT submit a half-typed command. When the menu is closed, everything falls
    through to normal Input behaviour.
    """

    def _menu(self) -> "OptionList | None":  # type: ignore[name-defined]
        try:
            return self.screen.query_one("#cmdmenu", OptionList)
        except Exception:  # noqa: BLE001 — menu not mounted yet
            return None

    async def _on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        menu = self._menu()
        if menu is not None and menu.display and event.key in _MENU_KEYS:
            if event.key == "down":
                menu.action_cursor_down()
            elif event.key == "up":
                menu.action_cursor_up()
            elif event.key == "escape":
                menu.display = False
            else:  # tab / enter → complete the highlight (Enter may also run it)
                hi = (menu.get_option_at_index(menu.highlighted)
                      if menu.highlighted is not None else None)
                exact = hi is not None and self.value.strip() == hi.id
                # Arg-completion rows carry the full line in their id ("/face sage") —
                # Enter on an exact match of one RUNS the command, like an arg-less one.
                if event.key == "enter" and exact and (hi.id in _ARGLESS or " " in hi.id):
                    menu.display = False
                    await super()._on_key(event)  # run the arg-less command now
                    return
                if hi is not None:
                    self.value = f"{hi.id} "
                    self.cursor_position = len(self.value)
                menu.display = False
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)


async def dispatch(app, text: str) -> bool:
    """Run a slash command against the cockpit ``app``. Returns True if ``text`` was a
    (handled) command so the caller does NOT also treat it as a chat turn. ``/council`` and
    plain text return False — the caller routes those to the chat client.

    This is the extended U4 static palette: the original set (``/help /profile /council
    /flight /refresh /prune /clear``), the T5 ``/theme /ascii /cost``, and the Wave 1B
    conversation commands (``/chats /new /rename``) plus pin switching (``/face /model``;
    ``/profile`` now sends a ``switch_profile`` frame instead of a local-only pin), plus
    the Wave 2 bot navigation (``/bots`` focuses the roster pane; ``/bot <slug>
    [message]`` binds the bot's canonical chat and optionally sends a normal turn).
    """
    log = app.query_one("#council", Log)

    if text in ("/help", "/help "):
        log.write_line("commands:")
        for c, d in SLASH_COMMANDS:
            log.write_line(f"  {c:<10} {d}")
        return True
    if text == "/chats":
        await app.action_open_chats()
        return True
    if text == "/bots":
        await app.action_open_bots()
        return True
    if text == "/bot" or text.startswith("/bot "):
        args = text[len("/bot"):].strip().split(None, 1)
        if not args:
            log.write_line("usage: /bot <slug> [message]")
            return True
        await app.open_bot_command(args[0], args[1] if len(args) > 1 else None)
        return True
    if text == "/new" or text.startswith("/new "):
        await app.new_conversation(text[len("/new"):].strip() or None)
        return True
    if text.startswith("/rename "):
        await app.rename_conversation(text.split(" ", 1)[1].strip())
        return True
    if text == "/clear":
        log.clear()
        return True
    if text == "/stop":
        await app.action_stop_generation()
        return True
    if text == "/theme":
        app.action_toggle_theme()
        return True
    if text == "/ascii":
        app.action_toggle_ascii()
        return True
    if text == "/cost":
        flights = app._state.flights()
        log.write_line(f"cost: {cost_line(flights)}")
        for v in flights:
            usd = float((getattr(v, "cost", None) or {}).get("usd") or 0.0)
            log.write_line(f"  {v.flight_id:<16} ${usd:.3f} EST · ~{fmt_tokens(v.tokens())} tok")
        return True
    if text == "/refresh":
        app.action_refresh()
        app.notify("refreshed routing / approvals / flights")
        return True
    if text == "/prune":
        app.action_prune()
        return True
    if text.startswith("/flight"):
        app.set_flight_filter(text[len("/flight"):].strip() or None)
        return True
    if text.startswith("/profile "):
        await app.switch_profile(text.split(" ", 1)[1].strip())
        return True
    if text.startswith("/face "):
        await app.switch_face(text.split(" ", 1)[1].strip())
        return True
    if text.startswith("/model "):
        args = text.split(" ", 1)[1].split()
        await app.switch_model(args[0], args[1] if len(args) > 1 else None)
        return True
    return False
