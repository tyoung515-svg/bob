"""BoBClaw TUI — ChatClient conversation binding tests (Wave 1B Task 1)
+ in-chat approval frames (Wave 1B Task 4).

The orphan fix: the active conversation id persists in ``.secrets/tui-state.json`` (0600,
same discipline as the token cache) and a second launch RESUMES it instead of minting a
fresh "cockpit agent" conversation. Tested with the real client over a fake session +
a tmp state file — no gateway, no Textual.
"""
from __future__ import annotations

import asyncio
import json
import stat

from tests.conftest import assert_private_mode
from types import SimpleNamespace

from bobclaw_tui.chat import ChatClient


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # (method, url, kwargs)

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert self._responses, f"unexpected call: {method} {url}"
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


def _chat(session, state_path) -> ChatClient:
    return ChatClient("127.0.0.1:7836", "tok", session, state_path=state_path)


def test_first_launch_creates_and_persists_0600(tmp_path):
    state = tmp_path / "tui-state.json"
    s = _FakeSession([_Resp(201, {"id": "c1", "title": "cockpit agent"})])
    cid = _run(_chat(s, state).ensure_conversation())
    assert cid == "c1"
    # persisted for the next launch, with the token-cache 0600 discipline
    assert json.loads(state.read_text()) == {"conversation_id": "c1", "title": "cockpit agent"}
    assert_private_mode(state)


def test_second_launch_resumes_same_conversation(tmp_path):
    """The orphan fix: launch 2 GET-probes the stored id and reuses it — NO new create."""
    state = tmp_path / "tui-state.json"
    s1 = _FakeSession([_Resp(201, {"id": "c1", "title": "cockpit agent"})])
    assert _run(_chat(s1, state).ensure_conversation()) == "c1"

    s2 = _FakeSession([_Resp(200, {"id": "c1", "title": "cockpit agent"})])
    chat2 = _chat(s2, state)
    assert _run(chat2.ensure_conversation()) == "c1"
    assert chat2.conversation_title == "cockpit agent"
    # exactly one GET probe, zero POSTs — the second launch created nothing
    assert [m for m, _, _ in s2.calls] == ["GET"]


def test_stored_id_gone_falls_back_to_create(tmp_path):
    """A 404 on the stored id (archived/deleted server-side) → fresh create + re-persist."""
    state = tmp_path / "tui-state.json"
    state.write_text(json.dumps({"conversation_id": "ghost", "title": "old"}))
    s = _FakeSession([
        _Resp(404, {}),                                        # probe: gone
        _Resp(201, {"id": "c2", "title": "cockpit agent"}),    # fallback create
    ])
    chat = _chat(s, state)
    assert _run(chat.ensure_conversation()) == "c2"
    assert json.loads(state.read_text())["conversation_id"] == "c2"


def test_create_failure_reports_and_returns_none(tmp_path):
    state = tmp_path / "tui-state.json"
    s = _FakeSession([_Resp(500, {"error": "boom"})])
    errors = []
    cid = _run(_chat(s, state).ensure_conversation(on_error=errors.append))
    assert cid is None
    assert errors and "conversation create failed" in errors[0]
    assert not state.exists()  # nothing persisted on failure


def test_new_and_rename_rebind_and_persist(tmp_path):
    state = tmp_path / "tui-state.json"
    s = _FakeSession([
        _Resp(201, {"id": "c1", "title": "my chat"}),
        _Resp(200, {"id": "c1", "title": "renamed chat"}),
    ])
    chat = _chat(s, state)
    conv = _run(chat.new_conversation("my chat"))
    assert conv["id"] == "c1" and chat.conversation_id == "c1"
    assert json.loads(state.read_text())["conversation_id"] == "c1"
    conv = _run(chat.rename("renamed chat"))
    assert chat.conversation_title == "renamed chat"
    assert json.loads(state.read_text())["title"] == "renamed chat"


def test_use_conversation_binds_picker_selection(tmp_path):
    state = tmp_path / "tui-state.json"
    chat = _chat(_FakeSession([]), state)
    cid = _run(chat.use_conversation({"id": "c9", "title": "picked"}))
    assert cid == "c9" and chat.conversation_title == "picked"
    assert json.loads(state.read_text())["conversation_id"] == "c9"


# ── Wave 1B Task 4: in-chat approvals over /ws/chat ──
class _FakeWS:
    """Records sent frames and replays canned server frames; an async context manager +
    async-iterable, like aiohttp's ClientWebSocketResponse."""

    def __init__(self, frames):
        self.sent = []
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_json(self, frame):
        self.sent.append(frame)

    def __aiter__(self):
        import aiohttp

        async def gen():
            for f in self._frames:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(f))

        return gen()


class _FakeWSSession:
    def __init__(self, ws):
        self._ws = ws

    def ws_connect(self, url, **kw):
        assert url.endswith("/ws/chat")
        return self._ws


def test_stream_turn_surfaces_approval_request(tmp_path):
    """An approval_request frame mid-stream goes to on_approval (action + details intact);
    chunks keep flowing and the turn still ends on message_complete."""
    ws = _FakeWS([
        {"type": "chunk", "content": "hold on — "},
        {"type": "approval_request", "approval_id": "abc123",
         "action": "email_send", "details": {"to": "travis@x.z"}},
        {"type": "message_complete"},
    ])
    chat = _chat(_FakeWSSession(ws), tmp_path / "state.json")
    chunks, prompts, errors = [], [], []
    _run(chat.stream_turn(
        "send it", council=False, profile=None, conversation_id="c1",
        on_chunk=chunks.append, on_error=errors.append, on_approval=prompts.append,
    ))
    assert chunks == ["hold on — "]
    assert errors == []
    assert prompts == [{"type": "approval_request", "approval_id": "abc123",
                        "action": "email_send", "details": {"to": "travis@x.z"}}]
    assert chat._turn_ws is None  # cleared when the turn socket closed


def test_answer_approval_sends_response_on_live_turn_socket(tmp_path):
    """The y/n answer rides the LIVE turn socket as an approval_response frame
    (approval_id + decision; no conversation_id — routers/chat keys the resume on the
    approval id only). No live socket → a clear error, not a silent no-op."""
    chat = _chat(_FakeWSSession(_FakeWS([])), tmp_path / "state.json")

    async def scenario():
        # no turn socket yet → refusing loudly beats a dropped decision
        try:
            await chat.answer_approval("abc123", True)
            raise AssertionError("expected RuntimeError without a live socket")
        except RuntimeError as exc:
            assert "no live turn socket" in str(exc)
        ws = _FakeWS([])
        chat._turn_ws = ws  # what stream_turn sets while the turn socket is open
        await chat.answer_approval("abc123", True)
        await chat.answer_approval("def456", False)
        return ws

    ws = _run(scenario())
    assert ws.sent == [
        {"type": "approval_response", "approval_id": "abc123", "decision": "approve"},
        {"type": "approval_response", "approval_id": "def456", "decision": "reject"},
    ]


# ── Wave 2 Task 3: the Bots-pane unread watermark shares the state file ──

def test_bot_last_seen_roundtrip_and_0600(tmp_path):
    state = tmp_path / "tui-state.json"
    chat = _chat(_FakeSession([]), state)
    assert chat.bot_last_seen() == {}                     # absent → empty, never raises
    chat.mark_bot_seen("helper", "2026-08-18T10:00:00")
    chat.mark_bot_seen("review", "2026-08-18T09:00:00")
    assert chat.bot_last_seen() == {"helper": "2026-08-18T10:00:00",
                                    "review": "2026-08-18T09:00:00"}
    # a fresh client over the same file sees the watermarks (persisted, 0600 discipline)
    assert _chat(_FakeSession([]), state).bot_last_seen()["helper"] == "2026-08-18T10:00:00"
    assert_private_mode(state)
    # a garbled watermark key degrades to empty instead of raising
    state.write_text(json.dumps({"bot_last_seen": "nonsense"}))
    assert chat.bot_last_seen() == {}


def test_watermark_and_conversation_binding_coexist(tmp_path):
    """The state file carries BOTH keys: marking a bot seen never clobbers the active
    conversation binding, and (re)binding a conversation never clobbers the watermarks."""
    state = tmp_path / "tui-state.json"
    s = _FakeSession([_Resp(201, {"id": "c1", "title": "cockpit agent"})])
    chat = _chat(s, state)
    assert _run(chat.ensure_conversation()) == "c1"
    chat.mark_bot_seen("helper", "2026-08-18T10:00:00")
    assert json.loads(state.read_text()) == {
        "conversation_id": "c1", "title": "cockpit agent",
        "bot_last_seen": {"helper": "2026-08-18T10:00:00"},
    }
    _run(chat.use_conversation({"id": "c2", "title": "Bot: helper"}))
    data = json.loads(state.read_text())
    assert data["conversation_id"] == "c2"                # rebound…
    assert data["bot_last_seen"] == {"helper": "2026-08-18T10:00:00"}  # …watermarks kept
