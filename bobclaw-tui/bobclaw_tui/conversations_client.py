"""BoBClaw TUI — /conversations REST client + pure row/history formatters (Wave 1B Task 1).

Thin async client over the gateway's conversation routes (patterned after
``monitor_client.py``: aiohttp, Bearer header, no Textual — so it is CI-testable with a
fake session). ``session`` is injectable (the cockpit's shared ``aiohttp.ClientSession``
in production, a fake in tests).

Gateway shapes (``bobclaw-gateway/routers/conversations.py``):

  * ``GET  /conversations``              → ``{"items": [...]}`` (newest ``updated_at`` first)
  * ``GET  /conversations/{id}``         → the conversation, 404 when gone/archived
  * ``POST /conversations``              → 201 + the created row
  * ``POST /conversations/{id}/rename``  → the renamed row
  * ``GET  /conversations/{id}/messages`` → cursor-paginated: ``{"items", "has_more"}``,
    each page **newest-first**, ``before=<oldest-seen message id>`` pages backward.

The pure formatters (``format_conversation_row`` / ``format_history_line``) live here too
— same discipline as ``panels.py``: JSON → display string, no I/O, unit-tested.
"""
from __future__ import annotations

from typing import Optional


def _base_url(gateway: str) -> str:
    return gateway if "://" in gateway else f"http://{gateway}"


class ConversationsClient:
    """Async REST client for the gateway's conversation routes.

    Raises ``RuntimeError`` on a non-success status (with the response body for context);
    transport errors (``aiohttp.ClientError``) propagate. ``get_conversation`` is the one
    exception: it returns ``None`` on any non-200, since callers use it as an
    exists-probe for the resume path.
    """

    def __init__(self, gateway: str, token: str, session) -> None:
        self._base = _base_url(gateway)
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"}

    async def list_conversations(self, *, limit: int = 50) -> list[dict]:
        """Non-archived conversations, newest ``updated_at`` first (server ordering)."""
        async with self._session.get(
            f"{self._base}/conversations", headers=self._headers, params={"limit": str(limit)}
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"list conversations failed {r.status}: {await r.text()}")
            data = await r.json(content_type=None)
        return data.get("items") or []

    async def get_conversation(self, conv_id: str) -> Optional[dict]:
        """The conversation, or ``None`` ONLY on a confirmed 404 (gone/archived).
        Any other failure raises — callers must be able to tell "gone" apart from
        "couldn't ask" (audit 1B task-1: a transient probe failure must not be
        treated as 'gone', or a blip clobbers the persisted binding)."""
        async with self._session.get(
            f"{self._base}/conversations/{conv_id}", headers=self._headers
        ) as r:
            if r.status == 404:
                return None
            if r.status != 200:
                raise RuntimeError(f"get conversation failed {r.status}: {await r.text()}")
            return await r.json(content_type=None)

    async def create(self, title: str, *, face_id: Optional[str] = "assistant") -> dict:
        """Create a conversation; returns the created row (201)."""
        body: dict = {"title": title}
        if face_id:
            body["face_id"] = face_id
        async with self._session.post(
            f"{self._base}/conversations", headers=self._headers, json=body
        ) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"create conversation failed {r.status}: {await r.text()}")
            return await r.json(content_type=None)

    async def rename(self, conv_id: str, title: str) -> dict:
        """Rename a conversation; returns the updated row."""
        async with self._session.post(
            f"{self._base}/conversations/{conv_id}/rename",
            headers=self._headers,
            json={"title": title},
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"rename conversation failed {r.status}: {await r.text()}")
            return await r.json(content_type=None)

    async def get_messages(self, conv_id: str, *, limit: int = 100, max_pages: int = 10) -> list[dict]:
        """Full message history, **oldest-first** (render order).

        The endpoint pages newest-first with a ``before`` cursor (the oldest message id of
        the page just read), so we walk pages backward and prepend each — the concatenation
        comes out oldest-first. ``max_pages`` caps the walk so a huge history can't stall
        the picker select.
        """
        items: list[dict] = []
        seen: set = set()
        before: Optional[str] = None
        truncated = False
        for _ in range(max_pages):
            params = {"limit": str(limit)}
            if before:
                params["before"] = before
            async with self._session.get(
                f"{self._base}/conversations/{conv_id}/messages",
                headers=self._headers,
                params=params,
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"get messages failed {r.status}: {await r.text()}")
                data = await r.json(content_type=None)
            page = data.get("items") or []
            # each page is newest-first → reverse it before prepending older pages,
            # so the concatenation comes out oldest-first (render order); dedup by id
            # defensively in case the cursor boundary is inclusive (audit 1B task-1)
            items = [m for m in reversed(page) if m.get("id") not in seen
                     and not seen.add(m.get("id"))] + items
            if not data.get("has_more") or not page:
                break
            before = str(page[-1].get("id"))
        else:
            truncated = True  # max_pages hit with (likely) more history above
        if truncated:
            items.insert(0, {"id": "__truncated__", "role": "system",
                             "content": "… older history truncated …"})
        return items


class AgentsClient:
    """Async REST client for the gateway's ``/agents`` routes + the ``/faces`` registry
    read (Wave 2 Task 4 — the ``/bot`` command's resolve-or-create path).

    Same error posture as :class:`ConversationsClient`: :meth:`get` returns ``None``
    ONLY on a confirmed 404 (any other failure raises, so "couldn't ask" is never
    mistaken for "no binding"); :meth:`create` raises on any non-201 (409 = slug
    conflict — the caller renders the error, never silently retries).
    """

    def __init__(self, gateway: str, token: str, session) -> None:
        self._base = _base_url(gateway)
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"}

    async def get(self, slug: str) -> Optional[dict]:
        """One binding by slug, or ``None`` on a confirmed 404. The slug is raw user
        text from ``/bot <slug>`` — URL-quote it so path chars can't rewrite the
        request (audit P2)."""
        from urllib.parse import quote

        async with self._session.get(
            f"{self._base}/agents/{quote(slug, safe='')}", headers=self._headers
        ) as r:
            if r.status == 404:
                return None
            if r.status != 200:
                raise RuntimeError(f"get agent failed {r.status}: {await r.text()}")
            return await r.json(content_type=None)

    async def create(self, slug: str, face_id: str, display_name: Optional[str] = None) -> dict:
        """Create the binding + canonical conversation (transactional gateway-side, 201)."""
        body: dict = {"slug": slug, "face_id": face_id}
        if display_name:
            body["display_name"] = display_name
        async with self._session.post(
            f"{self._base}/agents", headers=self._headers, json=body
        ) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"create agent failed {r.status}: {await r.text()}")
            return await r.json(content_type=None)

    async def list_faces(self) -> list:
        """The faces registry (``GET /faces``) as a bare list — ``[]`` on any failure,
        so a blip degrades to "no teammate face matched" (an unknown-bot line, never a
        crash). Dict wrappers (``{"faces": [...]}``) are unwrapped."""
        try:
            async with self._session.get(
                f"{self._base}/faces", headers=self._headers
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
        except Exception:  # noqa: BLE001 — unreachable gateway ⇒ no teammate match
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("faces") or data.get("items")
            return items if isinstance(items, list) else []
        return []


# ── pure formatters (no I/O — unit-tested) ──

_HISTORY_ROLE_LABELS = {"user": "you", "assistant": "bob"}


def format_conversation_row(conv: dict) -> str:
    """One picker row: ``title · updated-at`` (timestamp trimmed to the minute)."""
    title = str(conv.get("title") or "(untitled)")
    updated = str(conv.get("updated_at") or "")[:16]
    return f"{title} · {updated}" if updated else title


def format_history_line(msg: dict) -> str:
    """One history line with the same role prefixes the live turn render uses
    (``you›`` / ``bob›``), so resumed history reads like the live log."""
    role = str(msg.get("role") or "?")
    label = _HISTORY_ROLE_LABELS.get(role, role)
    return f"{label}› {msg.get('content') or ''}"
