"""BoBClaw TUI — /ws/chat client with per-turn streaming (MS6-T2 / T7) + conversation binding (Wave 1B Task 1).

The agent pane sends one turn over ``/ws/chat`` and, per T7, renders the reply **streamed**
— each ``chunk``/``token`` frame is handed to an ``on_chunk`` callback the moment it arrives,
so the app appends it to the log in place (``Log.write`` continues the current line) instead
of buffering the whole reply and dumping it at the end.

Kept a plain callback-driven client (no Textual) so the streaming path is testable with a
fake WS. Uses the cockpit's shared ``aiohttp.ClientSession`` (T2: one session for all REST/WS
app traffic). ``/ws/chat`` REQUIRES a ``conversation_id``; :meth:`ensure_conversation`
**resumes** the persisted one (or creates it once) instead of orphaning a fresh
"cockpit agent" conversation per launch:

  * The active conversation id (+ title) persists in ``.secrets/tui-state.json`` (0600 at
    creation, same discipline as the ``auth.py`` token cache).
  * On start, the stored id is probed (``GET /conversations/{id}``); a hit resumes it,
    a miss (404/gone) creates a fresh conversation and stores that.

Conversation management (``/chats`` picker, ``/new``, ``/rename``) goes through this client
too — it delegates the REST to :class:`~bobclaw_tui.conversations_client.ConversationsClient`
so the app only ever talks to one chat object (and the pilot suite fakes one object).

Pin switching (``/face``, ``/model``, ``/profile`` — Wave 1B Task 2) sends
``switch_face``/``switch_model``/``switch_profile`` frames, which REQUIRE a
``conversation_id`` (post-1A). WS architecture choice: this client opens a fresh socket per
turn (:meth:`stream_turn`), so a switch frame gets its own **short-lived control socket**
(:meth:`_control_roundtrip`) — open, send, read the ``*_switched`` ack, close. That is enough
because the gateway PERSISTS pins to the conversation row (they outlive any socket); the
alternative — piggybacking switch frames on the next turn socket — would give no immediate
ack and would interleave the ack with the turn stream. Local pin state mirrors the acks so
the status row can show the current pins.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from bobclaw_tui.conversations_client import AgentsClient, ConversationsClient
from bobclaw_tui.panels import _face_map, _is_teammate_face

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = _REPO_ROOT / ".secrets" / "tui-state.json"
_DEFAULT_TITLE = "cockpit agent"


def _read_state(state_path: Path | str) -> dict:
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state_path: Path | str, state: dict) -> None:
    """Persist the state file with mode 0600 set at creation (no chmod race window) —
    the same discipline as ``auth._write_cache``."""
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # fd-based (race-free): normalize a pre-existing loose file
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)


class ChatClient:
    """The agent pane's conversation over ``/ws/chat``, streamed — bound to a persisted
    conversation so launches resume instead of orphaning.

    ``session`` is the shared ``aiohttp.ClientSession``. ``ws_connect``/``post``/``get`` are
    taken from it, so a test can inject a fake session to exercise the streaming render path
    and the resume-or-create flow with no gateway. ``state_path``/``conv_client`` are
    injectable for tests.
    """

    def __init__(self, gateway: str, token: str, session, *,
                 state_path: Path | str = DEFAULT_STATE_PATH, conv_client=None,
                 agents_client=None) -> None:
        self._gateway = gateway
        self._token = token
        self._session = session
        self._headers = {"Authorization": f"Bearer {token}"}
        self._state_path = Path(state_path)
        self._convs = conv_client or ConversationsClient(gateway, token, session)
        self._agents = agents_client or AgentsClient(gateway, token, session)
        self._conv_id: Optional[str] = None
        self._conv_title: Optional[str] = None
        # active conversation's pins, mirrored from the gateway's *_switched acks
        self._face_pin: Optional[str] = None
        self._model_pin: Optional[str] = None
        self._backend_pin: Optional[str] = None
        self._profile_pin: Optional[str] = None
        # the live turn socket while stream_turn is reading (None between turns) — an
        # in-chat approval_request is answered on THIS socket (Wave 1B Task 4)
        self._turn_ws = None

    @property
    def conversation_id(self) -> Optional[str]:
        return self._conv_id

    @property
    def conversation_title(self) -> Optional[str]:
        return self._conv_title

    @property
    def face_pin(self) -> Optional[str]:
        return self._face_pin

    @property
    def model_pin(self) -> Optional[str]:
        return self._model_pin

    @property
    def backend_pin(self) -> Optional[str]:
        return self._backend_pin

    @property
    def profile_pin(self) -> Optional[str]:
        return self._profile_pin

    def _bind(self, conv: dict) -> str:
        """Make ``conv`` the active conversation and persist the binding. The write
        MERGES into the state file (it also carries ``bot_last_seen``, the Bots-pane
        unread watermark — Wave 2 Task 3), so a rebind never clobbers the watermarks."""
        self._conv_id = str(conv.get("id"))
        self._conv_title = conv.get("title")
        state = _read_state(self._state_path)
        state.update({"conversation_id": self._conv_id, "title": self._conv_title})
        _write_state(self._state_path, state)
        return self._conv_id

    # ── Bots pane unread watermark (Wave 2 Task 3) ──
    def bot_last_seen(self) -> dict:
        """``{slug: last-seen activity timestamp}`` from the state file (empty dict when
        absent/garbled). The Bots pane marks a bot unread when its binding activity is
        newer than this watermark."""
        seen = _read_state(self._state_path).get("bot_last_seen")
        return dict(seen) if isinstance(seen, dict) else {}

    def mark_bot_seen(self, slug: str, seen_at: str) -> None:
        """Advance one bot's watermark (on opening its chat) — merged into the state
        file like ``_bind``, so the active conversation binding survives the write."""
        state = _read_state(self._state_path)
        seen = state.get("bot_last_seen")
        if not isinstance(seen, dict):
            seen = {}
        seen[str(slug)] = str(seen_at)
        state["bot_last_seen"] = seen
        _write_state(self._state_path, state)

    async def ensure_conversation(self, on_error: Optional[Callable[[str], None]] = None) -> Optional[str]:
        """Resume the persisted conversation (or create it once) and cache its id.
        ``/ws/chat`` rejects a turn without a ``conversation_id``
        (``conversation_id and content are required``). Best-effort; returns None on
        failure (the caller aborts the turn)."""
        if self._conv_id:
            return self._conv_id
        state = _read_state(self._state_path)
        stored = state.get("conversation_id")
        if stored:
            try:
                conv = await self._convs.get_conversation(str(stored))
            except Exception as exc:  # noqa: BLE001 — transient probe failure (gateway
                # restart/timeout): do NOT mint+overwrite — resume the stored id
                # optimistically (audit 1B task-1: a blip must not clobber good state).
                if on_error:
                    on_error(f"resume probe failed ({exc}); keeping stored conversation")
                self._conv_id = str(stored)
                self._conv_title = state.get("title")
                return self._conv_id
            if conv is not None:
                return self._bind(conv)
        try:
            conv = await self._convs.create(_DEFAULT_TITLE)
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(f"conversation create failed: {exc}")
            return None
        return self._bind(conv)

    # ── conversation management (/chats picker, /new, /rename) ──
    async def list_conversations(self) -> list[dict]:
        """All conversations for the picker (newest first)."""
        return await self._convs.list_conversations()

    async def history(self) -> list[dict]:
        """Active conversation's messages, oldest-first (render order)."""
        if not self._conv_id:
            return []
        return await self._convs.get_messages(self._conv_id)

    async def use_conversation(self, conv: dict) -> str:
        """Switch the active conversation to a picker-selected one (persists)."""
        return self._bind(conv)

    async def new_conversation(self, title: Optional[str] = None) -> dict:
        """Create + switch (``/new [title]``; persists)."""
        conv = await self._convs.create(title or _DEFAULT_TITLE)
        self._bind(conv)
        return conv

    async def rename(self, title: str) -> dict:
        """Rename the active conversation (``/rename <title>``; persists)."""
        conv = await self._convs.rename(self._conv_id, title)
        self._bind(conv)
        return conv

    # ── bot resolve-or-create (/bot — Wave 2 Task 4) ──
    async def open_bot(self, slug: str) -> Optional[dict]:
        """Resolve ``/bot <slug>`` to the bot's binding.

        ``GET /agents/{slug}``; on a confirmed 404, a slug that names a TEAMMATE face
        (``bot: true`` or ``simple_slot`` — the same rule as the Bots pane roster)
        creates the binding via ``POST /agents`` (``face_id=slug``, ``display_name``
        from the face) and returns the created binding. Returns ``None`` when there is
        no binding AND the slug is not a teammate face — the caller renders the
        unknown-bot line and changes NO state. Fail-closed on creates: a ``/faces``
        blip (empty list) never mints a binding.
        """
        binding = await self._agents.get(slug)
        if binding is not None:
            return binding
        face = (_face_map(await self._agents.list_faces()) or {}).get(slug)
        if face is None or not _is_teammate_face(face):
            return None
        return await self._agents.create(
            slug, slug, str(face.get("display_name") or face.get("name") or slug))

    # ── pin switching (/face, /model, /profile — Wave 1B Task 2) ──
    async def _control_roundtrip(self, frame: dict, *, expect: str) -> dict:
        """Send ONE control frame on a short-lived ``/ws/chat`` socket and return the
        gateway's ack frame (see the module docstring for why a control socket, not the
        per-turn socket). Only the EXPECTED ack type closes the round-trip — an unrelated
        frame (stray notify, hello) is skipped, never mistaken for the ack (audit 1B
        task-2). Raises on an ``error`` frame, a malformed frame, or a close without ack.

        Back-to-back switches are last-ack-wins by construction; the DB pin is the
        source of truth, so a later correct command always repairs the state."""
        async with self._session.ws_connect(
            f"ws://{self._gateway}/ws/chat", headers=self._headers
        ) as ws:
            await ws.send_json(frame)
            async for msg in ws:
                if not _is_text(msg):
                    break
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    raise RuntimeError("malformed (non-JSON) frame from gateway") from None
                if data.get("type") == "error":
                    raise RuntimeError(data.get("message", "unknown"))
                if data.get("type") == expect:
                    return data
        raise RuntimeError("chat socket closed without an ack")

    async def switch_face(self, face_id: str, *, conversation_id: str) -> dict:
        """``/face <id>`` — pin a face on the conversation; the pin mirrors the
        ``face_switched`` ack (``conversation_id`` is required post-1A)."""
        ack = await self._control_roundtrip(
            {"type": "switch_face", "face_id": face_id, "conversation_id": conversation_id},
            expect="face_switched",
        )
        if ack.get("type") == "face_switched":
            self._face_pin = ack.get("face_id")
        return ack

    async def switch_model(self, model: Optional[str], backend: Optional[str], *,
                           conversation_id: str) -> dict:
        """``/model <model> [backend]`` — pin a model/backend on the conversation. The
        gateway keys the pin on ``backend`` (an empty backend CLEARS the pin back to
        auto routing, even with a model set), so the frame always carries both keys and
        the pins mirror the ``model_switched`` ack."""
        ack = await self._control_roundtrip(
            {"type": "switch_model", "model": model or "", "backend": backend or "",
             "conversation_id": conversation_id},
            expect="model_switched",
        )
        if ack.get("type") == "model_switched":
            self._model_pin = ack.get("model")
            self._backend_pin = ack.get("backend")
        return ack

    async def switch_profile(self, profile: str, *, conversation_id: str) -> dict:
        """``/profile <name>`` — pin a profile on the conversation; the pin mirrors the
        ``profile_switched`` ack."""
        ack = await self._control_roundtrip(
            {"type": "switch_profile", "profile": profile, "conversation_id": conversation_id},
            expect="profile_switched",
        )
        if ack.get("type") == "profile_switched":
            self._profile_pin = ack.get("profile")
        return ack

    # ── interrupt (Wave 1B Task 5) ──
    async def stop_generation(self, *, conversation_id: str) -> None:
        """Interrupt the live turn: ``stop_generation`` on the LIVE turn socket (the
        gateway keeps reading control frames while the stream task runs; post-1A the frame
        REQUIRES ``conversation_id``). The gateway answers with ``generation_stopped`` on
        the same stream, which :meth:`stream_turn` surfaces via ``on_stopped``. Raises when
        no turn socket is live (nothing to stop).

        The turn worker marks busy at schedule time but only assigns ``_turn_ws`` once the
        coroutine actually opens the socket — a fast /stop in that gap must not silently
        no-op, so poll briefly (bounded, ~0.5s) for the socket to appear."""
        import asyncio

        for _ in range(25):
            if self._turn_ws is not None:
                break
            await asyncio.sleep(0.02)
        if self._turn_ws is None:
            raise RuntimeError("no live turn socket — nothing to stop")
        await self._turn_ws.send_json(
            {"type": "stop_generation", "conversation_id": conversation_id}
        )

    # ── in-chat approvals (Wave 1B Task 4) ──
    async def answer_approval(self, approval_id: str, approved: bool) -> None:
        """Answer an in-chat ``approval_request`` on the LIVE turn socket — the gateway
        forwards the ``approval_response`` frame to core ``/api/chat/approval`` and the
        parked turn resumes on this same stream. The frame needs no ``conversation_id``
        (routers/chat keys the resume on ``approval_id`` only). Raises when no turn
        socket is live (the prompt arrived on a socket that already closed)."""
        if self._turn_ws is None:
            raise RuntimeError("no live turn socket — the approval can still be decided from the pane")
        await self._turn_ws.send_json({
            "type": "approval_response",
            "approval_id": approval_id,
            "decision": "approve" if approved else "reject",
        })

    async def stream_turn(
        self,
        prompt: str,
        *,
        council: bool,
        profile: Optional[str],
        conversation_id: str,
        on_chunk: Callable[[str], None],
        on_error: Callable[[str], None],
        on_approval: Optional[Callable[[dict], None]] = None,
        on_stopped: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """Fire one turn over ``/ws/chat`` and stream the reply.

        Each ``chunk`` (execute's message-level blob) or ``token`` (the council/streaming
        path) frame is passed to ``on_chunk`` as it arrives — that is the T7 per-turn stream.
        An ``approval_request`` frame is passed to ``on_approval`` (Wave 1B Task 4) and the
        stream keeps reading: the turn PARKS core-side until the human answers via
        :meth:`answer_approval` on this same socket. A ``generation_stopped`` frame (the
        gateway's answer to :meth:`stop_generation`, Wave 1B Task 5) is passed to
        ``on_stopped`` and ends the turn. ``on_error`` gets a single error string; the turn
        ends on ``message_complete`` or the socket closing. Honours the pinned ``profile``
        and the ``council`` trigger."""
        frame: dict = {"type": "message", "conversation_id": conversation_id, "content": prompt}
        if profile:
            frame["profile"] = profile
        if council:
            frame["face_id"] = "council-max"
        try:
            async with self._session.ws_connect(
                f"ws://{self._gateway}/ws/chat", headers=self._headers
            ) as ws:
                self._turn_ws = ws
                try:
                    await ws.send_json(frame)
                    async for msg in ws:
                        if not _is_text(msg):
                            break
                        data = json.loads(msg.data)
                        kind = data.get("type")
                        if kind in ("chunk", "token"):
                            on_chunk(data.get("content", ""))
                        elif kind == "approval_request":
                            if on_approval is not None:
                                on_approval(data)
                        elif kind == "error":
                            on_error(data.get("message", "unknown"))
                            break
                        elif kind == "generation_stopped":
                            if on_stopped is not None:
                                on_stopped(data)
                            break
                        elif kind == "message_complete":
                            break
                finally:
                    self._turn_ws = None
        except Exception as exc:  # noqa: BLE001
            on_error(f"chat error: {exc}")


def _is_text(msg) -> bool:
    """True if an aiohttp WS message is a TEXT frame (imported lazily so the module stays
    importable without aiohttp for pure-layer test collection)."""
    import aiohttp

    return msg.type == aiohttp.WSMsgType.TEXT
