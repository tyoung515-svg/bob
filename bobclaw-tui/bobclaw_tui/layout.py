"""BoBClaw TUI — cockpit layout: compose + CSS (MS6-T2 / T1 split).

The tmux-style grid and its stylesheet, split out of ``app.py``. The CSS references Textual
theme **variables** only (``$accent``, ``$warning``, ``$panel``, ``$text-muted``) — never raw
hex — so both BoBClaw theme variants (``theme.py``) restyle the whole cockpit live with no
widget changes (VOCABULARY §1 "no raw hex in widgets"). ``compose_cockpit`` yields the widget
tree; ``mount_layout`` wires the post-mount bits (DataTable columns + border titles).
"""
from __future__ import annotations

from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Log, OptionList, Static

from bobclaw_tui.commands import CommandInput

COCKPIT_CSS = """
#statusrow{ height: 1; padding: 0 1; color: $text-muted; background: $panel; }
#top      { height: 42%; }
#flights  { width: 1fr; border: round $accent; }
#sidebar  { width: 1fr; }
#routing  { height: 1fr; border: round $accent; }
#approvals{ height: 1fr; border: round $warning; }
#bots     { height: 1fr; border: round $accent; }
#mid      { height: 1fr; }
#fleet    { width: 1fr; border: round $accent; }
#council  { width: 1fr; border: round $accent; }
#cmdmenu  { display: none; height: auto; max-height: 8; border: round $accent; background: $panel; }
#chatpicker{ display: none; height: auto; max-height: 10; border: round $accent; background: $panel; }
#agent    { height: 3; border: round $accent; }
"""


def compose_cockpit(app):
    """Yield the cockpit widget tree (the app's ``compose`` delegates here).

    Row 1 = the connection-health + honest-cost + token-tick status row (T2/T3/T4), fed in
    ``_render`` from the two pure sources (reducer ``health()`` + ``ConnState``). Then the
    flights/sidebar grid, the fleet/council-log grid, the slash-command menu, and the agent
    input.
    """
    yield Header(show_clock=True)
    yield Static("● monitor: connecting", id="statusrow")
    with Horizontal(id="top"):
        yield DataTable(id="flights")
        with Vertical(id="sidebar"):
            yield Static("routing…", id="routing")
            yield Static("approvals…", id="approvals")
            yield OptionList(id="bots")
    with Horizontal(id="mid"):
        yield Static("no fleet activity yet", id="fleet")
        yield Log(id="council")
    yield OptionList(id="cmdmenu")
    yield OptionList(id="chatpicker")
    yield CommandInput(
        placeholder="agent>  type / for commands  ·  /profile <name>  ·  /council <prompt>",
        id="agent",
    )
    yield Footer()


def mount_layout(app) -> None:
    """Post-mount layout wiring: the flights DataTable columns and every pane's border title."""
    table = app.query_one("#flights", DataTable)
    table.add_columns("flight", "run", "done", "fail", "~tok", "ev")
    for wid, title in (
        ("#flights", "Flights"),
        ("#routing", "Routing · JOAT"),
        ("#approvals", "Approvals · Gate"),
        ("#bots", "Bots · teammates"),
        ("#fleet", "Fleet — orchestration + workers"),
        ("#council", "Agent / council log"),
        ("#cmdmenu", "commands"),
        ("#chatpicker", "chats"),
    ):
        app.query_one(wid).border_title = title
