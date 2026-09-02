"""BoBClaw TUI — the Textual cockpit (Lane 1c, view plane).

Renders the tested ``MonitorState`` (fed by ``/ws/monitor``) as per-flight panes, plus a
flights table (from ``/flights`` REST), a **Routing** pane (JOAT ``/routing-view`` poll), an
actionable **Approvals** pane (``/approvals`` poll + human-confirmed ``/decide`` — Wave 1B
Task 4, the only human-reachable gate surface now that gateway ``/ui`` is gone), and an
agent window over ``/ws/chat`` with a slash-command menu (``/`` pulls up the command
palette, like other TUIs).

MS6-T2 split this file (was 437 LOC / 5 concerns / zero app-layer tests) into modules — the
``App`` now only *composes* them:

  * ``layout``   — compose + CSS
  * ``commands`` — slash palette + dispatch (+ the ``CommandInput`` widget, U4 static set)
  * ``chat``     — ``/ws/chat`` client, per-turn **streamed** replies (T7)
  * ``pollers``  — REST loops over **one shared aiohttp session**
  * ``health``   — the connection/status-row wiring (T1)
  * ``theme``    — the BoBClaw dark/light Textual theme + the ``/ascii`` glyph table (T5)

The data plane (``monitor_state`` / ``panels`` / ``monitor_client``) is unchanged. Textual is
imported HERE (and in the presentation modules) only — the data layer never needs it, so it
stays CI-tested without a terminal, and the app layer is now exercised by a Textual-pilot
suite (``tests/test_app.py``). Run: ``python -m bobclaw_tui --token <jwt>``.
"""
from __future__ import annotations

import asyncio
import os
import time

try:
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Input, Log, OptionList, Static
    from textual.widgets.option_list import Option

    from bobclaw_tui import chat as chat_mod
    from bobclaw_tui import commands as commands_mod
    from bobclaw_tui import health as health_mod
    from bobclaw_tui import layout as layout_mod
    from bobclaw_tui import pollers as pollers_mod
    from bobclaw_tui import theme as theme_mod
    from bobclaw_tui.commands import SLASH_COMMANDS, CommandInput
    from bobclaw_tui.conversations_client import format_conversation_row, format_history_line
    _TEXTUAL = True
except Exception:  # noqa: BLE001 — Textual is an optional dep; the launcher installs it
    _TEXTUAL = False
    App = object  # type: ignore
    Input = object  # type: ignore

from bobclaw_tui.monitor_client import ConnState, stream_monitor
from bobclaw_tui.monitor_state import MonitorState
from bobclaw_tui.panels import (
    approval_prompt_lines, approvals_lines, decide_result_line, fleet_lines, fmt_tokens,
    switch_ack_line,
)


def _require_textual() -> None:
    if not _TEXTUAL:
        raise SystemExit(
            "textual is not installed. Run scripts/win/start-tui.ps1 (installs it) or "
            "`pip install textual`."
        )


DEFAULT_GATEWAY = os.getenv("BOBCLAW_GATEWAY", "127.0.0.1:7836")

# A finished (quiescent) flight is auto-evicted after this many seconds idle, so completed
# flights don't pile up forever. `x` / `/prune` clears them immediately.
_EVICT_IDLE_S = 90.0

# Enter on an empty line while a turn streams: twice within this window = /stop (Task 5).
_DOUBLE_ENTER_S = 1.5

_COCKPIT_CSS = layout_mod.COCKPIT_CSS if _TEXTUAL else ""


def _wrap_chunk(text: str, width: int, col: int = 0) -> tuple[str, int]:
    """Soft-wrap *text* to *width* starting at column *col* (2026-08-21).

    The agent log is a Textual ``Log``, which never wraps — long replies ran off
    the pane and needed horizontal scrolling. The stream writes token chunks with
    ``Log.write`` (in-place line continuation), so wrapping has to happen at write
    time: break at the last space inside the window when one exists, else hard-break
    at the width. Returns the text with newlines inserted plus the column the next
    chunk resumes at, so the caller can carry wrap state across streamed chunks."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\n":
            out.append("\n")
            col = 0
            i += 1
            continue
        room = width - col
        if room <= 0:
            out.append("\n")
            col = 0
            continue
        nxt = text.find("\n", i)
        seg = text[i:] if nxt == -1 else text[i:nxt]
        if len(seg) <= room:
            out.append(seg)
            col += len(seg)
            i += len(seg)
            continue
        cut = seg.rfind(" ", 0, room + 1)
        if cut <= 0:  # no space in the window — hard-break at the width
            out.append(seg[:room])
            out.append("\n")
            i += room
        else:  # the space becomes the line break
            out.append(seg[:cut])
            out.append("\n")
            i += cut + 1
        col = 0
    return "".join(out), col


class BobCockpit(App):  # type: ignore[misc]
    """The flight cockpit: fleet/council monitor + flights + routing + approvals + agent."""

    CSS = _COCKPIT_CSS
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh routing/approvals/flights"),
        ("x", "prune", "Prune finished flights"),
        ("t", "toggle_theme", "Theme dark/light"),
        ("j", "approval_next", "Next approval"),
        ("k", "approval_prev", "Prev approval"),
        ("a", "approve_selected", "Approve selected (confirm)"),
        ("d", "deny_selected", "Deny selected (confirm)"),
        # Input-safe variants: the plain letters above are swallowed as typed
        # text while the message input has focus (the default state). These fire
        # from anywhere — the Input widget already claims ctrl+a/d/k (cursor/
        # delete editing), hence ^↑/^↓/^y/^n/^r. (2026-08-21 focus-trap fix.)
        ("ctrl+down", "approval_next", "Next approval (input-safe)"),
        ("ctrl+up", "approval_prev", "Prev approval (input-safe)"),
        ("ctrl+y", "approve_selected", "Approve selected (input-safe)"),
        ("ctrl+n", "deny_selected", "Deny selected (input-safe)"),
        ("ctrl+r", "refresh", "Refresh (input-safe)"),
        ("escape", "cancel_pending_decision", "Cancel prompt / clear queue"),
    ]

    def __init__(
        self,
        gateway: str,
        token: str,
        flight_filter: str | None = None,
        *,
        autostart_io: bool = True,
        chat_client=None,
    ):
        super().__init__()
        self._gateway = gateway
        self._token = token
        self._flight_filter = flight_filter
        self._autostart_io = autostart_io      # pilot tests inject frames → no live sockets
        self._state = MonitorState()
        self._conn = ConnState()               # /ws/monitor socket health → header row (T1)
        self._stop = asyncio.Event()           # overall shutdown (panels loop + on_unmount)
        self._mon_stop = asyncio.Event()       # restartable — /flight re-subscribes the monitor
        self._flight_seen: dict = {}           # flight_id -> (events, monotonic_at_last_change)
        # presentation state (T5) — theme name + glyph mode, pinned from env, live-toggleable
        self._initial_theme = theme_mod.resolve_initial_theme() if _TEXTUAL else "bobclaw-dark"
        self._ascii_mode = theme_mod.resolve_initial_ascii() if _TEXTUAL else False
        self._status_text = ""                 # last rendered header row (health · cost · tok)
        self._session = None
        self._wrap_col = 0                     # agent-log soft-wrap column (see _wrap_chunk)
        self._chat = chat_client               # injectable for the pilot suite
        self._picker_convs: dict = {}          # /chats picker rows: id -> conversation dict
        self._chats_loading: bool = False      # /chats fetch in flight — ignore stale selects
        self._pollers = None
        self._caps = None                      # capabilities registry client (Task 3) — on_mount
        self._caps_fetch_started = False       # lazy: fetched once per session, first palette open
        self._caps_worker = None               # the lazy-fetch worker (tests await it)
        # approvals (Wave 1B Task 4): pending items from the last poll + the selected row,
        # and the ONE open decision prompt (pane confirm or in-chat approval_request)
        self._approval_items: list = []
        self._approval_sel: int = 0
        self._pending_decision: dict | None = None
        self._bot_items: list = []             # Bots pane rows from the last /agents poll (Wave 2 Task 3)
        self._decide_in_flight: bool = False  # a decide POST is in flight — block new a/d
        self._turn_worker = None               # the in-flight chat turn (Task 4: worker, not inline)
        # busy-turn queue (Wave 1B Task 5): (prompt, council) FIFO — input submitted while
        # a turn streams QUEUES instead of starting a concurrent turn (gateway-side a
        # second message would SUPERSEDE the live stream, so the queue is a UX choice)
        self._queue: list = []
        self._turn_stopped = False             # generation_stopped arrived → don't drain
        self._last_empty_enter = 0.0           # double-Enter while busy = /stop

    # ── layout (tmux-style grid) ──
    def compose(self) -> "ComposeResult":  # type: ignore[name-defined]
        yield from layout_mod.compose_cockpit(self)

    async def on_mount(self) -> None:
        # theme (T5): register the BoBClaw dark/light variants, apply the pinned/default one
        for th in theme_mod.BOBCLAW_THEMES:
            self.register_theme(th)
        self.theme = self._initial_theme

        layout_mod.mount_layout(self)

        # one shared aiohttp session for ALL cockpit REST/WS app traffic (T2)
        import aiohttp

        self._session = aiohttp.ClientSession()
        if self._chat is None:
            self._chat = chat_mod.ChatClient(self._gateway, self._token, self._session)
        self._pollers = pollers_mod.Pollers(self, self._gateway, self._token, self._session)
        self._caps = commands_mod.CapabilitiesClient(self._gateway, self._token, self._session)

        self.set_interval(0.5, self._render)          # redraw the stream-fed panes from state
        self.set_interval(2.0, self._reap_idle_flights)  # age out finished flights
        self._render()

        if self._autostart_io:
            self._monitor_task = asyncio.create_task(self._run_monitor())
            self._panels_task = asyncio.create_task(self._pollers.loop(self._stop))

    async def _run_monitor(self) -> None:
        """Stream ``/ws/monitor`` into the reducer until ``_mon_stop`` (restartable by /flight)."""
        await stream_monitor(
            f"ws://{self._gateway}/ws/monitor", self._token,
            self._state.apply, flight_id=self._flight_filter, stop=self._mon_stop,
            conn=self._conn,
        )

    # ── render the stream-fed panes (pure over MonitorState) ──
    def _render(self) -> None:
        flights = self._state.flights()
        # Header health/cost/token row (T2/T3/T4): reducer health() + socket ConnState +
        # fleet-wide EST cost/token totals — all pure formatting, glyphs per /ascii mode.
        self._status_text = health_mod.status_row_text(self)
        self.query_one("#statusrow", Static).update(self._status_text)
        sat = theme_mod.glyph("sat", self._ascii_mode)
        okg = theme_mod.glyph("ok", self._ascii_mode)
        table = self.query_one("#flights", DataTable)
        table.clear()
        for v in flights:
            tag = f"{sat} " if not v.is_ambient() else "· "
            # a resolved-glyph once quiescent (no running work) so a finished flight is
            # obvious — and signals it is now eligible for auto-eviction / prune.
            done_mark = f" {okg}" if v.is_quiescent() and (v.workers or v.council["seats"]) else ""
            table.add_row(
                f"{tag}{v.flight_id}{done_mark}", str(v.running()), str(v.done()),
                str(v.failed()), fmt_tokens(v.tokens()), str(v.events),
            )
        # Fleet pane = the two-tier orchestration tree (manager header + workers/seats).
        self.query_one("#fleet", Static).update(
            "\n".join(fleet_lines(flights, ascii_mode=self._ascii_mode))
        )

    def _reap_idle_flights(self) -> None:
        """Evict a flight once it's quiescent AND has been idle (no new frames) for
        ``_EVICT_IDLE_S`` — so completed flights don't accumulate. Activity (a rising
        ``events`` count) resets its idle timer. The clock lives HERE, not in the pure
        reducer; eviction is a plain ``MonitorState.evict``."""
        now = time.monotonic()
        for v in list(self._state.flights()):
            fid = v.flight_id
            seen = self._flight_seen.get(fid)
            if seen is None or seen[0] != v.events:
                self._flight_seen[fid] = (v.events, now)  # new activity → reset timer
                continue
            if now - seen[1] >= _EVICT_IDLE_S and v.is_quiescent():
                self._state.evict(fid)
                self._flight_seen.pop(fid, None)

    def action_prune(self) -> None:
        """Clear finished (quiescent) flights from the view immediately (`x` / /prune)."""
        pruned = 0
        for v in list(self._state.flights()):
            if v.is_quiescent():
                self._state.evict(v.flight_id)
                self._flight_seen.pop(v.flight_id, None)
                pruned += 1
        self.notify(f"pruned {pruned} finished flight(s)")

    def action_refresh(self) -> None:
        if self._pollers is None:
            return
        self.run_worker(self._pollers.poll_flights(), exclusive=True, group="flights")
        self.run_worker(self._pollers.refresh_panels(), group="panels")

    # ── approvals actions (Wave 1B Task 4) — the only human-reachable gate surface ──
    def _render_approvals_pane(self) -> None:
        """Re-render the Approvals pane from the last-polled items (selection marker moves
        with j/k); the items themselves only change on a poll/decide refresh."""
        n = len(self._approval_items)
        self._approval_sel = max(0, min(self._approval_sel, n - 1)) if n else 0
        self.query_one("#approvals", Static).update("\n".join(approvals_lines(
            {"items": self._approval_items}, ascii_mode=self._ascii_mode,
            selected=self._approval_sel)))

    def action_approval_next(self) -> None:
        if self._approval_items:
            self._approval_sel = min(self._approval_sel + 1, len(self._approval_items) - 1)
            self._render_approvals_pane()

    def action_approval_prev(self) -> None:
        if self._approval_items:
            self._approval_sel = max(self._approval_sel - 1, 0)
            self._render_approvals_pane()

    def action_approve_selected(self) -> None:
        self._start_pane_decision("approve")

    def action_deny_selected(self) -> None:
        self._start_pane_decision("reject")

    def _start_pane_decision(self, decision: str) -> None:
        """``a``/``d`` on the selected pending row opens a typed-y CONFIRM in the agent log —
        an accidental keypress must never decide the gate (nothing happens until a literal
        ``y`` is submitted; ``n``/Esc cancels)."""
        if self._pending_decision is not None or self._decide_in_flight:
            self.notify("finish the open approval prompt first (y/n, Esc cancels)")
            return
        if not self._approval_items:
            self.notify("no pending approvals")
            return
        item = self._approval_items[self._approval_sel]
        self._pending_decision = {"kind": "pane", "item": item, "decision": decision}
        log = self.query_one("#council", Log)
        log.write_line(
            f"confirm: {decision} {item.get('action_type') or '?'} "
            f"(id {str(item.get('id'))[:8]}…)? type y to confirm, n to cancel (Esc cancels)"
        )
        self.query_one("#agent", Input).focus()

    def action_cancel_pending_decision(self) -> None:
        """Esc dismisses the open approval prompt WITHOUT deciding (stays pending); with
        no prompt open, Esc CLEARS the busy-turn queue (Wave 1B Task 5, with a log note)."""
        if self._pending_decision is not None:
            self._pending_decision = None
            self.query_one("#council", Log).write_line(
                "approval prompt dismissed — no decision recorded")
            return
        if self._queue:
            n = len(self._queue)
            self._queue.clear()
            self.query_one("#council", Log).write_line(f"queue cleared ({n} dropped)")
            self._render()  # the queued count leaves the status row

    async def _resolve_pending_decision(self, answer: str) -> None:
        """``y``/``n`` on the open approval prompt. A pane confirm only executes on ``y``;
        an in-chat prompt sends the ``approval_response`` frame either way (y=approve,
        n=reject) — Esc (above) is the no-decision path."""
        pend = self._pending_decision
        self._pending_decision = None
        log = self.query_one("#council", Log)
        if pend["kind"] == "chat":
            approved = answer == "y"
            try:
                await self._chat.answer_approval(pend["approval_id"], approved)
            except Exception as exc:  # noqa: BLE001 — a blip must not kill the cockpit
                log.write_line(f"[approval answer failed: {exc}]")
                return
            log.write_line(f"approval answered: {'approved' if approved else 'rejected'}")
            return
        if answer != "y":
            log.write_line("approval cancelled — no decision recorded")
            return
        item, decision = pend["item"], pend["decision"]
        if self._pollers is None:
            log.write_line("[decide failed: pollers not running]")
            return
        # Hold an in-flight flag across the POST so a fast second a/d+confirm can't
        # fire a duplicate/conflicting decide for the same row before the pane
        # refreshes (audit 1B task-4 finding).
        self._decide_in_flight = True
        try:
            result = await self._pollers.decide(str(item.get("id")), decision)
        except Exception as exc:  # noqa: BLE001 — a gateway blip must not kill the cockpit
            log.write_line(f"[decide failed: {exc}]")
            return
        finally:
            self._decide_in_flight = False
        log.write_line(decide_result_line(result))
        await self._pollers.refresh_panels()  # the decided row leaves the pending pane

    def _open_chat_approval(self, frame: dict) -> None:
        """An in-chat ``approval_request`` frame → a y/n prompt in the agent log (the turn
        is parked core-side until answered; the frame arrives on the turn worker, never on
        the message pump)."""
        log = self.query_one("#council", Log)
        if self._pending_decision is not None:
            log.write_line(
                "[approval_request arrived while another prompt is open — decide it from the pane]")
            return
        self._pending_decision = {"kind": "chat",
                                  "approval_id": str(frame.get("approval_id") or "")}
        for ln in approval_prompt_lines(frame, ascii_mode=self._ascii_mode):
            log.write_line(ln)

    # ── presentation toggles (T5) — both apply live, no restart ──
    def action_toggle_theme(self) -> None:
        self.theme = theme_mod.next_theme(self.theme)
        self.notify(f"theme: {self.theme}")

    def action_toggle_ascii(self) -> None:
        self._ascii_mode = not self._ascii_mode
        self._render()  # re-render stream-fed panes now; poll-fed panes swap on next poll
        self.notify(f"glyphs: {'ascii' if self._ascii_mode else 'emoji'}")

    def set_flight_filter(self, flight_id: str | None) -> None:
        """Re-subscribe the monitor to one flight (empty = all) — /flight command."""
        self._flight_filter = flight_id
        self._mon_stop.set()                  # stop the old subscription
        self._mon_stop = asyncio.Event()      # fresh stop for the new one
        self._state = MonitorState()          # drop other flights from the view
        if self._autostart_io:
            self._monitor_task = asyncio.create_task(self._run_monitor())
        self.notify(f"monitor filter: {self._flight_filter or 'all flights'}")

    # ── slash-command menu (pull up like other TUIs) ──
    def _ensure_capabilities(self) -> None:
        """Kick the one-per-session capabilities fetch (lazy — on the first palette open;
        Wave 1B Task 3). A failure is silent — the palette keeps the static list."""
        if self._caps is None or self._caps_fetch_started:
            return
        self._caps_fetch_started = True
        self._caps_worker = self.run_worker(self._caps.fetch(), exclusive=True, group="caps")

    def on_input_changed(self, event) -> None:  # type: ignore[no-untyped-def]
        if getattr(event.input, "id", None) != "agent":
            return
        self._refresh_command_menu(event.value or "")

    def _refresh_command_menu(self, val: str) -> None:
        menu = self.query_one("#cmdmenu", OptionList)
        # Show only while typing the command token (a leading "/", no space yet).
        if val.startswith("/") and " " not in val:
            self._ensure_capabilities()
            matches = [(c, d) for c, d in SLASH_COMMANDS if c.startswith(val)]
            menu.clear_options()
            if matches:
                for c, d in matches:
                    menu.add_option(Option(f"{c:<10} {d}", id=c))
                menu.highlighted = 0
                menu.display = True
            else:
                menu.display = False
        else:
            # Argument completion (Wave 1B Task 3): /face /profile /model offer the
            # fetched registry ids in the same popup; empty when unavailable (silent).
            # Wave 2 Task 4: /bot completes slugs from the polled Bots-pane roster.
            opts = self._caps.completions(val) if self._caps is not None else []
            if not opts:
                opts = commands_mod.bot_arg_completions(self._bot_items, val)
            menu.clear_options()
            if opts:
                for oid, label in opts:
                    menu.add_option(Option(label, id=oid))
                menu.highlighted = 0
                menu.display = True
            else:
                menu.display = False

    def on_worker_state_changed(self, event) -> None:  # type: ignore[no-untyped-def]
        """Audit 1B task-3 fixes: when the lazy capabilities fetch finishes, (1) unlatch
        on failure so the next palette open retries, and (2) re-render the menu for the
        current input so a late-arriving fetch still populates an open popup."""
        if getattr(event.worker, "group", None) != "caps":
            return
        if getattr(event.state, "name", "") not in ("SUCCESS", "ERROR", "CANCELLED"):
            return
        if self._caps is not None and not getattr(self._caps, "available", True):
            self._caps_fetch_started = False  # failed fetch — allow retry on next open
        try:
            val = self.query_one("#agent", Input).value or ""
        except Exception:  # noqa: BLE001 — widget unmounted/mid-teardown: nothing to refresh
            return
        self._refresh_command_menu(val)

    async def on_option_list_option_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        """OptionList activation: the slash menu fills the command into the line; the
        ``/chats`` picker binds the selected conversation and renders its history; the
        Bots pane binds the selected teammate's canonical conversation (Wave 2 Task 3)."""
        lid = getattr(event.option_list, "id", None)
        if lid == "bots":
            await self._open_bot(str(event.option.id or ""))
            return
        if lid == "chatpicker":
            if self._chats_loading:
                return  # a refresh is mid-flight — ignore stale-picker selects
            picker = self.query_one("#chatpicker", OptionList)
            picker.display = False
            self.query_one("#agent", Input).focus()
            conv = self._picker_convs.get(str(event.option.id))
            if conv is None:
                return
            await self._chat.use_conversation(conv)
            self._render()  # status row picks up the new active title
            await self._render_history()
            return
        if lid != "cmdmenu":
            return
        inp = self.query_one("#agent", CommandInput)
        inp.value = f"{event.option.id} "
        inp.cursor_position = len(inp.value)
        self.query_one("#cmdmenu", OptionList).display = False
        inp.focus()

    # ── conversation binding (Wave 1B Task 1): /chats picker + /new + /rename ──
    async def _open_bot(self, slug: str) -> None:
        """Bots-pane select (Wave 2 Task 3): bind the selected teammate's canonical
        conversation from the last-polled roster (``_bot_items``). A stale slug or an
        unbound row is a no-op — the pane never navigates away by accident."""
        binding = next((b for b in self._bot_items if str(b.get("slug")) == slug), None)
        if binding is not None:
            await self._bind_bot(slug, binding)

    async def _bind_bot(self, slug: str, binding: dict) -> bool:
        """Shared tail of bot navigation (pane select + ``/bot``): bind the canonical
        conversation (the SAME ``use_conversation`` + history path as the /chats picker
        — navigation, no second dispatch engine) and advance the unread watermark to
        the activity the operator just opened. Returns False (no state change) when
        the binding has no canonical conversation."""
        if not binding.get("conversation_id"):
            return False
        conv = {"id": str(binding["conversation_id"]),
                "title": binding.get("display_name") or f"Bot: {slug}"}
        await self._chat.use_conversation(conv)
        mark = getattr(self._chat, "mark_bot_seen", None)
        if callable(mark):
            mark(slug, str(binding.get("updated_at") or ""))
        self.query_one("#agent", Input).focus()
        self._render()  # status row picks up the new active title
        await self._render_history()
        return True

    async def open_bot_command(self, slug: str, message: str | None = None) -> None:
        """``/bot <slug> [message]`` (Wave 2 Task 4): switch the active conversation to
        the bot's canonical chat — ``ChatClient.open_bot`` resolves the binding,
        creating it via ``POST /agents`` on first use when the slug names a teammate
        face — render history, then send ``message`` as a NORMAL turn (the existing
        ``_send_chat`` path, no second dispatch engine). An unknown slug that is not a
        teammate face is a clear log line and NO state change."""
        log = self.query_one("#council", Log)
        try:
            binding = await self._chat.open_bot(slug)
        except Exception as exc:  # noqa: BLE001 — a gateway blip must not kill the cockpit
            log.write_line(f"[/bot {slug} failed: {exc}]")
            return
        if binding is None:
            log.write_line(
                f"unknown bot: {slug} — no binding, and not a teammate face "
                "(the Bots pane lists the roster)")
            return
        bound = await self._bind_bot(slug, binding)
        if not bound:
            # Never send into the previously-open conversation when the bind
            # failed (audit P2: discarded _bind_bot result).
            log.write_line(f"[/bot {slug}: binding has no conversation — nothing sent]")
            return
        if message:
            await self._send_chat(message, council=False)

    async def action_open_bots(self) -> None:
        """``/bots`` — focus the Bots pane (the teammate roster; ↑/↓ + Enter binds the
        highlighted bot's canonical chat), kicking a refresh so the roster is current.
        Consistency with ``/chats``: the command opens the surface, selection binds."""
        self.query_one("#bots", OptionList).focus()
        if self._autostart_io and self._pollers is not None:
            self.run_worker(self._pollers.refresh_panels(), group="panels")

    async def action_open_chats(self) -> None:
        """``/chats`` — list conversations (newest first) into the picker popup.
        Clear state + mark loading BEFORE the network await so a select fired
        against the stale picker can't bind an outdated row (audit 1B task-1)."""
        if self._chats_loading:
            return
        self._chats_loading = True
        picker = self.query_one("#chatpicker", OptionList)
        picker.clear_options()
        self._picker_convs = {}
        picker.display = False
        try:
            convs = await self._chat.list_conversations()
        except Exception as exc:  # noqa: BLE001 — a gateway blip must not kill the cockpit
            self.notify(f"chats list failed: {exc}", severity="error")
            return
        finally:
            self._chats_loading = False
        if not convs:
            picker.display = False
            self.notify("no conversations yet — /new starts one")
            return
        for c in convs:
            cid = str(c.get("id"))
            self._picker_convs[cid] = c
            picker.add_option(Option(format_conversation_row(c), id=cid))
        picker.highlighted = 0
        picker.display = True
        picker.focus()

    async def _render_history(self) -> None:
        """Render the active conversation's messages (oldest-first, role prefixes) into
        the agent log — the resume view after a /chats select."""
        log = self.query_one("#council", Log)
        try:
            msgs = await self._chat.history()
        except Exception as exc:  # noqa: BLE001
            log.write_line(f"[history load failed: {exc}]")
            return
        log.write_line(f"— {self._chat.conversation_title or 'conversation'} —")
        for m in msgs:
            # history lines wrap independently (write_lines commits each one)
            _w = max(20, log.size.width - 2)
            log.write_lines(_wrap_chunk(format_history_line(m), _w)[0].split("\n"))
        log.write("\n")  # blank spacer (write_line("") is a no-op: "".splitlines() == [])

    async def new_conversation(self, title: str | None) -> None:
        """``/new [title]`` — create + switch; the log restarts on the fresh conversation."""
        log = self.query_one("#council", Log)
        try:
            conv = await self._chat.new_conversation(title)
        except Exception as exc:  # noqa: BLE001
            log.write_line(f"[new conversation failed: {exc}]")
            return
        self._render()
        log.write_line(f"— new conversation: {conv.get('title')} —")

    async def rename_conversation(self, title: str) -> None:
        """``/rename <title>`` — rename the active conversation."""
        log = self.query_one("#council", Log)
        try:
            conv = await self._chat.rename(title)
        except Exception as exc:  # noqa: BLE001
            log.write_line(f"[rename failed: {exc}]")
            return
        self._render()
        self.notify(f"renamed: {conv.get('title') or title}")

    # ── pin switching (Wave 1B Task 2): /face + /model + /profile ──
    async def _switch_pin(self, verb: str, switch) -> None:
        """Shared body for the pin commands: ensure a conversation, send the switch frame
        (which carries the conversation_id), render the ack, refresh the status-row pins."""
        log = self.query_one("#council", Log)
        cid = await self._chat.ensure_conversation(on_error=lambda m: log.write_line(f"[{m}]"))
        if not cid:
            log.write_line(f"[no conversation; /{verb} aborted]")
            return
        try:
            ack = await switch(cid)
        except Exception as exc:  # noqa: BLE001 — a gateway blip must not kill the cockpit
            log.write_line(f"[{verb} switch failed: {exc}]")
            return
        log.write_line(switch_ack_line(ack))
        self._render()  # status row picks up the new pins

    async def switch_face(self, face_id: str) -> None:
        """``/face <id>`` — pin a face on the active conversation (WS ``switch_face``)."""
        await self._switch_pin("face", lambda cid: self._chat.switch_face(face_id, conversation_id=cid))

    async def switch_model(self, model: str, backend: str | None) -> None:
        """``/model <model> [backend]`` — pin a model (+ optional backend) on the active
        conversation (WS ``switch_model``; an empty backend clears the pin gateway-side)."""
        await self._switch_pin(
            "model", lambda cid: self._chat.switch_model(model, backend, conversation_id=cid))

    async def switch_profile(self, profile: str) -> None:
        """``/profile <name>`` — pin a profile on the active conversation (WS
        ``switch_profile``); later turns also carry it in the message frame."""
        await self._switch_pin(
            "profile", lambda cid: self._chat.switch_profile(profile, conversation_id=cid))

    # ── agent window (Q4: profile pin + council trigger + commands) ──
    async def on_input_submitted(self, event) -> None:  # type: ignore[no-untyped-def]
        text = (event.value or "").strip()
        self.query_one("#agent", Input).value = ""
        self.query_one("#cmdmenu", OptionList).display = False
        if not text:
            await self._maybe_stop_on_double_enter()
            return
        # an open approval prompt owns the input line: y/n resolve it, everything else is
        # swallowed (never sent as a turn) until the prompt is answered or Esc-dismissed
        if self._pending_decision is not None:
            low = text.lower()
            if low in ("y", "n"):
                await self._resolve_pending_decision(low)
            else:
                self.notify("approval prompt open — answer y or n (Esc dismisses)",
                            severity="warning")
            return
        if await commands_mod.dispatch(self, text):
            return
        convene = text.startswith("/council")
        prompt = text[len("/council"):].strip() if convene else text
        if not prompt:
            return
        await self._send_chat(prompt, council=convene)

    # ── busy-turn queue + interrupt (Wave 1B Task 5) ──
    def _turn_busy(self) -> bool:
        """True while a chat-turn worker is alive (a reply is streaming, or a queued drain
        is in progress) — submitted input QUEUES instead of starting a concurrent turn."""
        w = self._turn_worker
        return w is not None and not w.is_finished

    async def _maybe_stop_on_double_enter(self) -> None:
        """Enter on an EMPTY line while a turn streams: the first warns, the second (within
        ``_DOUBLE_ENTER_S``) stops the turn — the muscle-memory interrupt, same as /stop."""
        if not self._turn_busy():
            return
        now = time.monotonic()
        if now - self._last_empty_enter <= _DOUBLE_ENTER_S:
            self._last_empty_enter = 0.0
            await self.action_stop_generation()
        else:
            self._last_empty_enter = now
            self.notify("turn streaming — Enter again to stop (typed input queues)")

    async def action_stop_generation(self) -> None:
        """``/stop`` (or double-Enter while busy): interrupt the streaming turn.

        Sends ``stop_generation`` WITH ``conversation_id`` (required post-1A) on the LIVE
        turn socket; the gateway's ``generation_stopped`` reply renders inline via
        :meth:`_on_generation_stopped`. The queue is NOT auto-drained into the stopped
        turn — it stays queued (status-row count) until the next submitted turn completes
        and drains it naturally (or Esc clears it)."""
        log = self.query_one("#council", Log)
        if not self._turn_busy():
            self.notify("no turn streaming — nothing to stop")
            return
        cid = await self._chat.ensure_conversation(on_error=lambda m: log.write_line(f"[{m}]"))
        if not cid:
            log.write_line("[no conversation; stop aborted]")
            return
        try:
            await self._chat.stop_generation(conversation_id=cid)
        except Exception as exc:  # noqa: BLE001 — a blip must not kill the cockpit
            log.write_line(f"[stop failed: {exc}]")

    def _on_generation_stopped(self, frame: dict) -> None:
        """The gateway's ``generation_stopped`` frame (the answer to ``stop_generation``
        on the live turn socket, post-1A) renders inline on the agent line and marks the
        turn stopped so :meth:`_stream_reply` does NOT drain the queue into it."""
        self._turn_stopped = True
        self._log_wrap_write(self.query_one("#council", Log), " [stopped]")

    def _log_wrap_write(self, log: "Log", text: str) -> None:
        """Write *text* to the agent log soft-wrapped to the pane's inner width.

        ``Log`` never wraps on its own (long replies ran off the pane — the
        2026-08-21 wrap fix). Wrap state rides ``self._wrap_col`` so streamed
        token chunks continue the in-progress line correctly."""
        width = max(20, log.size.width - 2)  # the round border eats 2 cells
        if self._wrap_col == 0 and log.lines and str(log.lines[-1]):
            # ``Log.write`` always continues the LAST line, even one committed by
            # ``write_line`` — at column 0 with a non-empty tail we must break
            # onto a fresh line first or the text glues itself to that line.
            text = "\n" + text
        wrapped, self._wrap_col = _wrap_chunk(text, width, self._wrap_col)
        log.write(wrapped)

    async def _send_chat(self, prompt: str, *, council: bool) -> None:
        """Fire one turn over /ws/chat, honoring the pinned profile / council trigger, and
        render the reply **streamed** (T7): the prompt line is committed, then each arriving
        chunk/token is appended in place to the agent line (``Log.write`` continues the
        current line) instead of buffering the whole reply and dumping it at the end.

        The stream itself runs in a WORKER (Wave 1B Task 4): Textual awaits message
        handlers inline, so awaiting the turn here would freeze input for the whole turn —
        and an in-chat ``approval_request`` parks the turn core-side until the human types
        y/n, which is only reachable if the message pump stays live.

        While a turn streams, input QUEUES (Wave 1B Task 5) instead of starting a
        concurrent turn: gateway-side a second ``message`` would SUPERSEDE the live stream,
        so the FIFO queue is a UX choice — the queued text is logged, the status row shows
        the count, Esc clears it, and :meth:`_stream_reply` drains it on completion."""
        if self._turn_busy():
            self._queue.append((prompt, council))
            self.query_one("#council", Log).write_line(f"queued ({len(self._queue)}): {prompt}")
            self._render()  # status row picks up the queued count now
            return
        log = self.query_one("#council", Log)
        cid = await self._chat.ensure_conversation(on_error=lambda m: log.write_line(f"[{m}]"))
        if not cid:
            log.write_line("[no conversation; turn aborted]")
            return
        # the profile pin lives on the chat client (set by /profile's switch_profile ack);
        # getattr keeps pin-unaware fake chat clients in the pilot suite working
        profile_pin = getattr(self._chat, "profile_pin", None)
        label = "council" if council else (profile_pin or "bob")
        who = "you› " + ("[council] " if council else "")
        # prompt line, then START the assistant line (trailing "\n" commits the prompt line
        # so streamed chunks land on the labelled agent line, not merged onto the prompt).
        self._wrap_col = 0
        self._log_wrap_write(log, f"{who}{prompt}\n{label}› ")
        self._turn_stopped = False  # a fresh turn clears a prior stop's drain-block
        self._last_empty_enter = 0.0  # a fresh turn also clears any armed double-Enter warning
        self._turn_worker = self.run_worker(
            self._stream_reply(prompt, council=council, profile=profile_pin, conversation_id=cid),
            exclusive=True, group="chat")

    async def _stream_reply(self, prompt: str, *, council: bool, profile, conversation_id: str) -> None:
        """The worker half of :meth:`_send_chat`: stream the turn, render chunks in place,
        surface ``approval_request`` frames as a y/n prompt, terminate the agent line —
        then DRAIN the busy-turn queue FIFO (Wave 1B Task 5): each queued message sends as
        its own turn, in order, inside this same worker (so :attr:`_turn_worker` stays busy
        for the whole drain and ``wait()`` covers it). A stopped turn
        (``generation_stopped``) does NOT drain: the queue stays queued until the next
        submitted turn completes naturally (or Esc clears it)."""
        log = self.query_one("#council", Log)
        while True:
            got = {"any": False}

            def on_chunk(t: str) -> None:
                if t:
                    got["any"] = True
                    self._log_wrap_write(log, t)

            def on_error(m: str) -> None:
                got["any"] = True
                self._log_wrap_write(log, f"[error: {m}]")

            await self._chat.stream_turn(
                prompt, council=council, profile=profile, conversation_id=conversation_id,
                on_chunk=on_chunk, on_error=on_error, on_approval=self._open_chat_approval,
                on_stopped=self._on_generation_stopped,
            )
            if not got["any"] and not self._turn_stopped:
                self._log_wrap_write(log, "(no reply)")
            self._log_wrap_write(log, "\n\n")  # terminate the agent line + a blank spacer
            if self._turn_stopped or not self._queue:
                break
            prompt, council = self._queue.pop(0)
            self._render()  # status row reflects the remaining queue per drained item
            profile = getattr(self._chat, "profile_pin", None)
            label = "council" if council else (profile or "bob")
            who = "you› " + ("[council] " if council else "")
            self._wrap_col = 0
            self._log_wrap_write(log, f"{who}{prompt}\n{label}› ")
        self._render()  # the queued count leaves the status row

    async def on_unmount(self) -> None:
        self._stop.set()
        self._mon_stop.set()
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass


def run(gateway: str, token: str, flight_filter: str | None = None) -> None:
    _require_textual()
    BobCockpit(gateway, token, flight_filter).run()
