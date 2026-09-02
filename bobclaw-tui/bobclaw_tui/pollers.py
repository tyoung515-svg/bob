"""BoBClaw TUI — REST poll loops (MS6-T2 / T1 module split).

The Routing and Approvals panes (and the /flights registry probe) are fed by REST polls,
not the ``/ws/monitor`` stream. This module owns those loops and, per the T2 contract,
**one shared ``aiohttp.ClientSession``** for the whole cockpit's REST traffic (the old
``app.py`` opened a fresh session on every 5 s tick). The JSON→lines shaping stays PURE in
``panels`` — this module only does the aiohttp I/O + ``Static.update`` and is fail-open: a
gateway/Postgres blip renders an ``unavailable`` line, never crashes the cockpit.

Posture (§6.7): read-only EXCEPT the one human-gated write — ``decide()`` POSTs
``/approvals/{id}/decide`` when the operator confirms an approve/deny in the cockpit
(Wave 1B Task 4; the gateway ``/ui`` is gone, so this restores the only human-reachable
approvals surface). Fleet control stays untouched.
"""
from __future__ import annotations

import asyncio

from bobclaw_tui.panels import approvals_lines, bots_lines, bot_rows, routing_lines

_PANEL_POLL_S = 5.0


class Pollers:
    """REST-fed panes over one shared session. ``app`` is the cockpit (for widget queries +
    the live glyph mode); ``session`` is the shared ``aiohttp.ClientSession`` created once by
    the app on mount and closed on unmount."""

    def __init__(self, app, gateway: str, token: str, session) -> None:
        self._app = app
        self._gateway = gateway
        self._token = token
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"}

    async def loop(self, stop: asyncio.Event) -> None:
        """Refresh the REST-fed panes every ``_PANEL_POLL_S`` until ``stop``, promptly."""
        while not stop.is_set():
            await self.refresh_panels()
            try:
                await asyncio.wait_for(stop.wait(), timeout=_PANEL_POLL_S)
            except asyncio.TimeoutError:
                pass

    async def refresh_panels(self) -> None:
        """One poll of ``/routing-view`` + ``/approvals`` + ``/agents`` + ``/faces`` → the
        sidebar panes (fail-open). The Bots pane (Wave 2 Task 3) is an OptionList — one
        row per teammate binding, Enter binds its canonical conversation."""
        try:
            routing = await self._get_json("/routing-view")
            approvals = await self._get_json("/approvals?status=pending")
            agents = await self._get_json("/agents")
            faces = await self._get_json("/faces")
        except Exception as exc:  # noqa: BLE001 — a poll never kills the cockpit
            routing = approvals = agents = faces = {"error": str(exc)}
        ascii_mode = self._app._ascii_mode
        # Track the pending items + selection on the app so the a/d approve/deny
        # keybinds (Wave 1B Task 4) act on the row the operator sees selected.
        items = approvals.get("items") if isinstance(approvals, dict) else None
        self._app._approval_items = items or []
        # Clamp the selection EVERY poll, not just on j/k: a row decided elsewhere
        # shrinks the list, and an unclamped trailing cursor would IndexError the
        # a/d decide path (audit 1B task-4 finding).
        n = len(self._app._approval_items)
        sel = getattr(self._app, "_approval_sel", 0)
        sel = max(0, min(sel, n - 1)) if n else 0
        self._app._approval_sel = sel
        self._app.query_one("#routing").update(
            "\n".join(routing_lines(routing, ascii_mode=ascii_mode))
        )
        self._app.query_one("#approvals").update(
            "\n".join(approvals_lines(approvals, ascii_mode=ascii_mode, selected=sel))
        )
        self._render_bots(agents, faces, ascii_mode)

    def _render_bots(self, agents, faces, ascii_mode: bool) -> None:
        """Repopulate the Bots OptionList from the latest ``/agents`` + ``/faces`` poll.
        The app keeps the rendered bindings (``_bot_items``) so a select can bind the
        canonical conversation; the unread watermark comes from the chat client's state
        file (fake chat clients without it degrade to no marks)."""
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        rows = bot_rows(agents, faces)
        self._app._bot_items = rows or []
        seen_fn = getattr(getattr(self._app, "_chat", None), "bot_last_seen", None)
        last_seen = seen_fn() if callable(seen_fn) else {}
        lines = bots_lines(agents, faces, last_seen, ascii_mode=ascii_mode)
        pane = self._app.query_one("#bots", OptionList)
        keep = (str(pane.get_option_at_index(pane.highlighted).id)
                if pane.highlighted is not None and pane.option_count else None)
        pane.clear_options()
        if self._app._bot_items:
            for binding, line in zip(self._app._bot_items, lines[1:]):
                pane.add_option(Option(line, id=str(binding.get("slug"))))
        else:
            for line in lines:  # error / empty-roster message lines (not selectable)
                pane.add_option(Option(line))
        slugs = [str(b.get("slug")) for b in self._app._bot_items]
        pane.highlighted = slugs.index(keep) if keep in slugs else (0 if slugs else None)

    async def decide(self, approval_id: str, decision: str) -> dict:
        """POST ``/approvals/{id}/decide`` (``decision`` = ``approve``/``reject``) and
        return the response dict (``status`` + ``agent_resume`` …). Raises on transport
        failure or a non-dict body — the caller renders the error; the decide is the one
        human-gated write this module performs."""
        async with self._session.post(
            f"http://{self._gateway}/approvals/{approval_id}/decide",
            json={"decision": decision}, headers=self._headers,
        ) as r:
            data = await r.json(content_type=None)
            if not isinstance(data, dict):
                raise RuntimeError(f"unexpected decide body: {data!r}")
            if r.status >= 400:
                raise RuntimeError(str(data.get("error") or f"HTTP {r.status}"))
            return data

    async def _get_json(self, path: str) -> dict:
        """GET ``path`` off the gateway on the shared session → the JSON body
        (``{'error': ...}`` on any failure). ``content_type=None`` so an error body served
        as text/plain still parses. Dict AND list bodies pass through (``/faces`` is a
        bare list); any other body is wrapped so the formatters' guards hold."""
        try:
            async with self._session.get(
                f"http://{self._gateway}{path}", headers=self._headers
            ) as r:
                data = await r.json(content_type=None)
                if isinstance(data, (dict, list)):
                    return data
                return {"error": f"unexpected body: {data!r}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def poll_flights(self) -> None:
        """Best-effort: pull the flight registry (names/budgets) from /flights REST → notify."""
        try:
            async with self._session.get(
                f"http://{self._gateway}/flights", headers=self._headers
            ) as r:
                data = await r.json(content_type=None)
                items = data.get("items", []) if isinstance(data, dict) else []
                self._app.notify(f"{len(items)} flights registered")
        except Exception as exc:  # noqa: BLE001
            self._app.notify(f"flights fetch failed: {exc}", severity="error")
