"""BoBClaw TUI — Textual-pilot app-layer suite (MS6-T2 / T1 "kill the zero-tests gap").

Drives the real ``BobCockpit`` via ``App.run_test()`` — the app layer had **zero** automated
tests before this sprint (only the pure data layer was covered). These exercise the things
the module split touches: startup compose, a slash-command dispatch, the ``/theme`` and
``/ascii`` toggles, per-turn streamed chat (T7), and — the §5 E2E-equivalent — a header
health-row transition (idle→DOWN→live) driven by injected frames (no live gateway, per
invariant §5), across **both themes and both glyph modes** (the §6 acceptance).

Hermetic: ``autostart_io=False`` means no ``/ws/monitor`` socket / REST loop spawns — we feed
the reducer + ``ConnState`` directly, exactly the sanctioned reducer-injection smoke.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from textual.widgets import DataTable, Input, Log, OptionList, Static

from bobclaw_tui.app import BobCockpit, _wrap_chunk


def _run(scenario) -> None:
    """Run an async pilot scenario on a fresh event loop (no pytest-asyncio dependency)."""
    asyncio.run(scenario())


def _cockpit(**kw) -> BobCockpit:
    return BobCockpit("127.0.0.1:7826", "test-token", autostart_io=False, **kw)


# ── a fake chat client to exercise the T7 streaming render path with no gateway ──
class _FakeChat:
    def __init__(self, chunks):
        self._chunks = chunks
        self.conv = "conv-1"
        self.turns = 0
        self.mid = None       # log snapshot captured right after the FIRST chunk arrives
        self.app = None       # set by the test so we can inspect the live Log mid-stream

    async def ensure_conversation(self, on_error=None):
        return self.conv

    async def stream_turn(self, prompt, *, council, profile, conversation_id, on_chunk, on_error,
                          on_approval=None, on_stopped=None):
        self.turns += 1
        assert conversation_id == self.conv
        for i, c in enumerate(self._chunks):
            on_chunk(c)  # streamed: each chunk handed over the moment it "arrives"
            if i == 0 and self.app is not None:
                # capture the Log the instant the first chunk rendered — a buffered
                # (non-streaming) revert would NOT have the partial agent line here yet.
                self.mid = list(self.app.query_one("#council", Log).lines)


# ── startup compose ──
def test_startup_composes_all_panes():
    async def scenario():
        app = _cockpit()
        async with app.run_test():
            # every cockpit pane mounted
            for wid, cls in (
                ("#statusrow", Static), ("#flights", DataTable), ("#routing", Static),
                ("#approvals", Static), ("#fleet", Static), ("#council", Log),
                ("#cmdmenu", OptionList), ("#agent", Input),
            ):
                assert app.query_one(wid, cls) is not None
            # the flights table has its columns; the header rendered once on mount
            assert len(app.query_one("#flights", DataTable).columns) == 6
            assert app._status_text.startswith("● monitor:")
            # default theme applied (dark, unpinned)
            assert app.theme == "bobclaw-dark"

    _run(scenario)


# ── one slash-command dispatch (/help) ──
def test_slash_help_dispatch_writes_commands():
    async def scenario():
        app = _cockpit()
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/help"))
            lines = list(app.query_one("#council", Log).lines)
            assert "commands:" in lines
            # the new T5 commands are surfaced in the palette help
            joined = "\n".join(lines)
            assert "/theme" in joined and "/ascii" in joined and "/cost" in joined

    _run(scenario)


# ── the / palette pop-up populates ──
def test_slash_menu_populates_on_prefix():
    async def scenario():
        app = _cockpit()
        async with app.run_test():
            app.on_input_changed(SimpleNamespace(input=SimpleNamespace(id="agent"), value="/c"))
            menu = app.query_one("#cmdmenu", OptionList)
            assert menu.display is True
            assert menu.option_count >= 2  # /council, /clear, /cost

    _run(scenario)


# ── /theme toggle (dark ⇄ light), live ──
def test_theme_toggle_live():
    async def scenario():
        app = _cockpit()
        async with app.run_test():
            assert app.theme == "bobclaw-dark"
            await app.on_input_submitted(SimpleNamespace(value="/theme"))
            assert app.theme == "bobclaw-light"
            await app.on_input_submitted(SimpleNamespace(value="/theme"))
            assert app.theme == "bobclaw-dark"

    _run(scenario)


# ── /ascii toggle swaps the glyph set, live ──
def test_ascii_toggle_swaps_glyphs():
    async def scenario():
        app = _cockpit()
        async with app.run_test():
            assert app._ascii_mode is False
            assert app._status_text.startswith("● monitor:")
            await app.on_input_submitted(SimpleNamespace(value="/ascii"))
            assert app._ascii_mode is True
            # the health dot fell back to ASCII 'o' immediately (no restart)
            assert app._status_text.startswith("o monitor:")
            await app.on_input_submitted(SimpleNamespace(value="/ascii"))
            assert app._ascii_mode is False
            assert app._status_text.startswith("● monitor:")

    _run(scenario)


# ── T7: per-turn streamed reply lands on the labelled agent line ──
def test_chat_reply_streams_onto_agent_line():
    async def scenario():
        fake = _FakeChat(["Hel", "lo ", "world"])
        app = _cockpit(chat_client=fake)
        fake.app = app
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="hi there"))
            await app._turn_worker.wait()  # turns stream in a worker (Wave 1B Task 4)
            lines = list(app.query_one("#council", Log).lines)
            assert "you› hi there" in lines
            # the three chunks were appended in place → one labelled, streamed agent line
            assert any(ln == "bob› Hello world" for ln in lines)
            assert app._chat.turns == 1
            # PROOF of per-chunk streaming (catches a revert to buffered rendering): after
            # only the FIRST chunk arrived, the partial agent line was already in the Log,
            # and the full reply was NOT — impossible if chunks were buffered to the end.
            assert fake.mid is not None
            assert "bob› Hel" in fake.mid
            assert "bob› Hello world" not in fake.mid

    _run(scenario)


def test_council_trigger_streams_council_label():
    async def scenario():
        app = _cockpit(chat_client=_FakeChat(["deliberating"]))
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/council should we ship?"))
            await app._turn_worker.wait()  # turns stream in a worker (Wave 1B Task 4)
            lines = list(app.query_one("#council", Log).lines)
            assert any(ln.startswith("you› [council] should we ship?") for ln in lines)
            assert any(ln == "council› deliberating" for ln in lines)

    _run(scenario)


# ── health-row transition idle→DOWN→live (reducer-injection E2E, invariant §5) ──
def test_health_row_transition_idle_down_live():
    async def scenario():
        app = _cockpit()
        async with app.run_test():
            app._conn.note_connected()
            app._state.apply({"type": "worker_state", "flight_id": "r", "idx": 0,
                              "status": "ok", "backend": "d", "tokens": 900})
            app._state.apply({"type": "cost", "flight_id": "r", "usd": 0.05})
            app._render()
            assert app._status_text.startswith("● monitor: live")
            assert "$0.050 EST" in app._status_text and "~900 tok" in app._status_text

            # Redis dies mid-run → header DOWN (wins even though the socket is still live)
            app._state.apply({"type": "error", "code": "redis_unavailable", "message": "x"})
            app._render()
            assert "● monitor: DOWN:redis_unavailable" in app._status_text
            assert app._conn.status == "live"

            # recovery: the next healthy frame clears the transport error → back to live
            app._state.apply({"type": "worker_state", "flight_id": "r", "idx": 1,
                              "status": "ok", "backend": "d", "tokens": 600})
            app._render()
            assert app._status_text.startswith("● monitor: live")
            assert "~1.5k tok" in app._status_text  # 900 + 600 aggregated

    _run(scenario)


# ── §6 acceptance: a headless cockpit run on BOTH themes AND BOTH glyph modes ──
def test_cockpit_runs_both_themes_and_glyph_modes():
    async def scenario():
        for theme_name in ("bobclaw-dark", "bobclaw-light"):
            for ascii_mode in (False, True):
                app = _cockpit()
                async with app.run_test():
                    app.theme = theme_name
                    app._ascii_mode = ascii_mode
                    dot = "o" if ascii_mode else "●"
                    # idle→DOWN→live health transition renders in this theme+glyph combo
                    app._conn.note_connected()
                    app._state.apply({"type": "worker_state", "flight_id": "f", "idx": 0,
                                      "status": "ok", "backend": "d", "tokens": 900})
                    app._render()
                    assert app._status_text.startswith(f"{dot} monitor: live")
                    app._state.apply({"type": "error", "code": "redis_unavailable",
                                      "message": "x"})
                    app._render()
                    assert "DOWN:redis_unavailable" in app._status_text
                    app._state.apply({"type": "worker_state", "flight_id": "f", "idx": 1,
                                      "status": "ok", "backend": "d", "tokens": 600})
                    app._render()
                    assert app._status_text.startswith(f"{dot} monitor: live")
                    # theme actually applied + the cockpit composed
                    assert app.theme == theme_name
                    assert app.query_one("#flights", DataTable) is not None
                    assert app.query_one("#fleet", Static) is not None

    _run(scenario)


# ── /ascii + /theme are honored from the env pin at construction ──
def test_env_pins_theme_and_ascii(monkeypatch):
    async def scenario():
        monkeypatch.setenv("BOBCLAW_TUI_THEME", "light")
        monkeypatch.setenv("BOBCLAW_TUI_ASCII", "1")
        app = _cockpit()
        async with app.run_test():
            assert app.theme == "bobclaw-light"
            assert app._ascii_mode is True
            assert app._status_text.startswith("o monitor:")

    _run(scenario)


# ── Wave 1B Task 1: conversation binding — /new, /chats picker + history, /rename ──
class _FakeConvo:
    """_FakeChat-style fake with the conversation-management surface the app drives."""

    def __init__(self):
        self.conv_id = "conv-1"
        self.title = "first chat"
        self.convs = [
            {"id": "conv-2", "title": "second chat", "updated_at": "2026-08-18T10:00:00"},
            {"id": "conv-1", "title": "first chat", "updated_at": "2026-08-18T09:00:00"},
        ]
        self.history_msgs = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        self.new_calls = []
        self.rename_calls = []

    @property
    def conversation_id(self):
        return self.conv_id

    @property
    def conversation_title(self):
        return self.title

    async def ensure_conversation(self, on_error=None):
        return self.conv_id

    async def list_conversations(self):
        return list(self.convs)

    async def history(self):
        return list(self.history_msgs)

    async def use_conversation(self, conv):
        self.conv_id = str(conv["id"])
        self.title = conv.get("title")
        return self.conv_id

    async def new_conversation(self, title=None):
        self.new_calls.append(title)
        self.conv_id = f"conv-new-{len(self.new_calls)}"
        self.title = title or "cockpit agent"
        return {"id": self.conv_id, "title": self.title}

    async def rename(self, title):
        self.rename_calls.append(title)
        self.title = title
        return {"id": self.conv_id, "title": title}


def test_new_command_creates_and_switches():
    async def scenario():
        fake = _FakeConvo()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/new ops standup"))
            assert fake.new_calls == ["ops standup"]
            assert fake.conversation_id == "conv-new-1"          # switched
            assert "chat: ops standup" in app._status_text       # status row picks it up
            lines = list(app.query_one("#council", Log).lines)
            assert "— new conversation: ops standup —" in lines

    _run(scenario)


def test_chats_picker_populates_and_select_renders_history():
    async def scenario():
        fake = _FakeConvo()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/chats"))
            picker = app.query_one("#chatpicker", OptionList)
            assert picker.display is True
            assert picker.option_count == 2
            # rows are title + trimmed updated_at, newest first
            assert str(picker.get_option_at_index(0).prompt) == "second chat · 2026-08-18T10:00"

            # select the second chat → bind + history render + picker dismissed
            await app.on_option_list_option_selected(SimpleNamespace(
                option_list=SimpleNamespace(id="chatpicker"),
                option=SimpleNamespace(id="conv-2"),
            ))
            assert picker.display is False
            assert fake.conversation_id == "conv-2"
            assert "chat: second chat" in app._status_text
            lines = list(app.query_one("#council", Log).lines)
            assert "— second chat —" in lines
            # history rendered oldest-first with the live-log role prefixes
            assert "you› earlier question" in lines
            assert "bob› earlier answer" in lines

    _run(scenario)


def test_rename_command_renames_active():
    async def scenario():
        fake = _FakeConvo()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/rename fresh title"))
            assert fake.rename_calls == ["fresh title"]
            assert fake.conversation_title == "fresh title"
            assert "chat: fresh title" in app._status_text

    _run(scenario)


# ── the orphan fix, pilot-level: a second launch resumes the persisted conversation ──
class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        import json as _json
        return _json.dumps(self._payload)


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert self._responses, f"unexpected call: {method} {url}"
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


def test_second_launch_resumes_active_conversation(tmp_path):
    """Launch 1 creates + persists; launch 2 (a NEW client over the same state file)
    resumes the same id with ONE GET probe and ZERO creates — no orphaned conversation."""
    from bobclaw_tui.chat import ChatClient

    state = tmp_path / "tui-state.json"

    async def scenario():
        s1 = _FakeSession([_Resp(201, {"id": "c1", "title": "cockpit agent"})])
        app1 = _cockpit(chat_client=ChatClient("127.0.0.1:7836", "tok", s1, state_path=state))
        async with app1.run_test():
            assert await app1._chat.ensure_conversation() == "c1"

        s2 = _FakeSession([_Resp(200, {"id": "c1", "title": "cockpit agent"})])
        app2 = _cockpit(chat_client=ChatClient("127.0.0.1:7836", "tok", s2, state_path=state))
        async with app2.run_test():
            assert await app2._chat.ensure_conversation() == "c1"   # same id resumed
            assert app2._chat.conversation_title == "cockpit agent"
            app2._render()
            assert "chat: cockpit agent" in app2._status_text
            assert [m for m, _, _ in s2.calls] == ["GET"]           # probe only, no create

    _run(scenario)


# ── Wave 1B Task 2: pin switching — /face, /model, /profile ──
class _FakePinChat:
    """_FakeChat-style fake with the pin-switching surface the app drives. Records the
    exact frame each switch method would send (the app's only contract with the client)
    and mirrors the pins from the acks it returns, like the real ChatClient."""

    def __init__(self):
        self.conv_id = "conv-1"
        self.title = "first chat"
        self.frames = []          # exact frames sent, in order
        self.face_pin = None
        self.model_pin = None
        self.backend_pin = None
        self.profile_pin = None

    @property
    def conversation_id(self):
        return self.conv_id

    @property
    def conversation_title(self):
        return self.title

    async def ensure_conversation(self, on_error=None):
        return self.conv_id

    async def switch_face(self, face_id, *, conversation_id):
        self.frames.append({"type": "switch_face", "face_id": face_id,
                            "conversation_id": conversation_id})
        self.face_pin = face_id
        return {"type": "face_switched", "face_id": face_id, "face_name": face_id}

    async def switch_model(self, model, backend, *, conversation_id):
        self.frames.append({"type": "switch_model", "model": model or "",
                            "backend": backend or "", "conversation_id": conversation_id})
        if not backend:  # gateway: empty backend clears the pin
            self.model_pin = self.backend_pin = None
            return {"type": "model_switched", "model": None, "backend": None}
        self.model_pin, self.backend_pin = model, backend
        return {"type": "model_switched", "model": model, "backend": backend}

    async def switch_profile(self, profile, *, conversation_id):
        self.frames.append({"type": "switch_profile", "profile": profile,
                            "conversation_id": conversation_id})
        self.profile_pin = profile
        return {"type": "profile_switched", "profile": profile}


def test_face_command_sends_frame_and_renders_ack():
    async def scenario():
        fake = _FakePinChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/face sage"))
            # exact frame, conversation_id included (required by the gateway post-1A)
            assert fake.frames == [{"type": "switch_face", "face_id": "sage",
                                    "conversation_id": "conv-1"}]
            lines = list(app.query_one("#council", Log).lines)
            assert "face pinned: sage" in lines          # ack rendered in the agent log
            assert "face: sage" in app._status_text      # pin visible in the status row

    _run(scenario)


def test_model_command_sends_frame_with_backend_and_renders_ack():
    async def scenario():
        fake = _FakePinChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/model gpt-5 openai"))
            assert fake.frames == [{"type": "switch_model", "model": "gpt-5",
                                    "backend": "openai", "conversation_id": "conv-1"}]
            lines = list(app.query_one("#council", Log).lines)
            assert "model pinned: gpt-5 @ openai" in lines
            assert "model: gpt-5@openai" in app._status_text

    _run(scenario)


def test_model_command_without_backend_clears_pin():
    """The gateway keys the model pin on backend: an empty backend acks model/backend
    None — the pin is cleared back to auto routing and the status row drops the cell."""
    async def scenario():
        fake = _FakePinChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/model gpt-5 openai"))
            assert "model: gpt-5@openai" in app._status_text
            await app.on_input_submitted(SimpleNamespace(value="/model gpt-5"))
            assert fake.frames[-1] == {"type": "switch_model", "model": "gpt-5",
                                       "backend": "", "conversation_id": "conv-1"}
            lines = list(app.query_one("#council", Log).lines)
            assert "model unpinned (auto routing)" in lines
            assert "model:" not in app._status_text

    _run(scenario)


def test_profile_command_sends_frame_and_renders_ack():
    async def scenario():
        fake = _FakePinChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/profile council-max"))
            assert fake.frames == [{"type": "switch_profile", "profile": "council-max",
                                    "conversation_id": "conv-1"}]
            lines = list(app.query_one("#council", Log).lines)
            assert "profile pinned: council-max" in lines
            assert "profile: council-max" in app._status_text

    _run(scenario)


def test_profile_pin_labels_later_turns():
    """After /profile pins, the next turn carries the pin (message frame) and the agent
    line is labelled with it."""
    async def scenario():
        fake = _FakePinChat()
        fake.streamed = None

        async def stream_turn(prompt, *, council, profile, conversation_id, on_chunk, on_error,
                              on_approval=None, on_stopped=None):
            fake.streamed = {"prompt": prompt, "profile": profile,
                             "conversation_id": conversation_id}
            on_chunk("ack")

        fake.stream_turn = stream_turn
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/profile ops-team"))
            await app.on_input_submitted(SimpleNamespace(value="status?"))
            await app._turn_worker.wait()  # turns stream in a worker (Wave 1B Task 4)
            assert fake.streamed == {"prompt": "status?", "profile": "ops-team",
                                     "conversation_id": "conv-1"}
            lines = list(app.query_one("#council", Log).lines)
            assert "ops-team› ack" in lines

    _run(scenario)


# ── the wire-level proof: a real ChatClient over a fake WS sends the exact frames ──
class _FakeWS:
    """Records sent frames and replays canned acks; an async context manager +
    async-iterable, like aiohttp's ClientWebSocketResponse."""

    def __init__(self, acks):
        self.sent = []
        self._acks = iter(acks)  # consumed across roundtrips: one ack per control socket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_json(self, frame):
        self.sent.append(frame)

    def __aiter__(self):
        import aiohttp
        import json as _json

        async def gen():
            for a in self._acks:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=_json.dumps(a))

        return gen()


class _FakeWSSession:
    def __init__(self, ws):
        self._ws = ws

    def ws_connect(self, url, **kw):
        assert url.endswith("/ws/chat")
        return self._ws


def test_switch_frames_carry_conversation_id_on_wire():
    """ChatClient.switch_* on a short-lived control socket: exact frames (conversation_id
    REQUIRED post-1A), pins mirrored from the acks, error frame raises."""
    from bobclaw_tui.chat import ChatClient

    async def scenario():
        ws = _FakeWS([
            {"type": "face_switched", "face_id": "sage", "face_name": "sage"},
            {"type": "model_switched", "model": "gpt-5", "backend": "openai"},
            {"type": "profile_switched", "profile": "council-max"},
        ])
        chat = ChatClient("127.0.0.1:7836", "tok", _FakeWSSession(ws),
                          state_path="/nonexistent/tui-state.json")
        ack = await chat.switch_face("sage", conversation_id="conv-1")
        assert ack == {"type": "face_switched", "face_id": "sage", "face_name": "sage"}
        await chat.switch_model("gpt-5", "openai", conversation_id="conv-1")
        await chat.switch_profile("council-max", conversation_id="conv-1")
        assert ws.sent == [
            {"type": "switch_face", "face_id": "sage", "conversation_id": "conv-1"},
            {"type": "switch_model", "model": "gpt-5", "backend": "openai",
             "conversation_id": "conv-1"},
            {"type": "switch_profile", "profile": "council-max", "conversation_id": "conv-1"},
        ]
        assert chat.face_pin == "sage"
        assert chat.model_pin == "gpt-5" and chat.backend_pin == "openai"
        assert chat.profile_pin == "council-max"

        err_ws = _FakeWS([{"type": "error", "message": "Conversation not found or access denied",
                           "code": "not_found"}])
        chat2 = ChatClient("127.0.0.1:7836", "tok", _FakeWSSession(err_ws),
                           state_path="/nonexistent/tui-state.json")
        try:
            await chat2.switch_face("sage", conversation_id="gone")
            raise AssertionError("expected the error frame to raise")
        except RuntimeError as exc:
            assert "Conversation not found" in str(exc)
        assert chat2.face_pin is None  # a failed switch leaves the local pin untouched

    _run(scenario)


# ── Wave 1B Task 3: dynamic palette augmentation (capabilities arg completion) ──
class _CapsResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _CapsSession:
    """Replays queued responses, recording calls — same discipline as _FakeWSSession."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        assert self._responses, f"unexpected call: GET {url}"
        return self._responses.pop(0)


_CAPS_DOC = {
    "faces": [
        {"id": "assistant", "display_name": "Assistant", "blurb": "the default helper"},
        {"id": "sage", "display_name": None, "blurb": "deep thinker"},
    ],
    "backends": [
        {"backend": "lmstudio", "model": "qwen3-8b", "available": True},
        {"backend": "openai", "model": "gpt-5", "available": True},
    ],
}
_PROFILES_DOC = {"items": [{"name": "council-max", "builtin": True}]}


def _caps_client() -> "commands.CapabilitiesClient":
    from bobclaw_tui import commands
    return commands.CapabilitiesClient(
        "127.0.0.1:7836", "tok",
        _CapsSession([_CapsResp(200, _CAPS_DOC), _CapsResp(200, _PROFILES_DOC)]),
    )


def test_arg_completion_popup_offers_registry_ids():
    """``/face `` / ``/model `` with a prefix fill the SAME #cmdmenu popup with the
    fetched registry ids (option id = the full line, so accepting it fills the input);
    an unfetched (unavailable) registry leaves the popup hidden — silent degrade."""
    async def scenario():
        caps = _caps_client()
        await caps.fetch()
        app = _cockpit()
        async with app.run_test():
            app._caps = caps
            menu = app.query_one("#cmdmenu", OptionList)
            app.on_input_changed(SimpleNamespace(input=SimpleNamespace(id="agent"),
                                                 value="/face s"))
            assert menu.display is True
            assert [menu.get_option_at_index(i).id for i in range(menu.option_count)] == \
                ["/face sage"]
            app.on_input_changed(SimpleNamespace(input=SimpleNamespace(id="agent"),
                                                 value="/model q"))
            assert [menu.get_option_at_index(i).id for i in range(menu.option_count)] == \
                ["/model qwen3-8b lmstudio"]
            # unavailable registry (never fetched) ⇒ no popup, static palette only
            app._caps = _caps_client()
            app.on_input_changed(SimpleNamespace(input=SimpleNamespace(id="agent"),
                                                 value="/face s"))
            assert menu.display is False

    _run(scenario)


def test_palette_open_kicks_lazy_caps_fetch_once_per_session():
    """Typing ``/`` starts the one-per-session fetch (lazy — not on mount); further
    palette opens do not re-fetch."""
    class _FakeCaps:
        def __init__(self):
            self.fetches = 0

        async def fetch(self):
            self.fetches += 1

        def completions(self, value):
            return []

    async def scenario():
        app = _cockpit()
        async with app.run_test():
            fake = _FakeCaps()
            app._caps = fake
            assert app._caps_fetch_started is False     # not fetched on mount
            app.on_input_changed(SimpleNamespace(input=SimpleNamespace(id="agent"),
                                                 value="/"))
            assert app._caps_fetch_started is True
            await app._caps_worker.wait()
            app.on_input_changed(SimpleNamespace(input=SimpleNamespace(id="agent"),
                                                 value="/c"))
            assert fake.fetches == 1                    # once per session, no hammering

    _run(scenario)


def test_enter_on_exact_arg_completion_submits():
    """UX: with the popup showing the exact arg completion (``/face sage`` fully typed),
    Enter RUNS the command instead of re-completing it."""
    async def scenario():
        fake = _FakePinChat()
        caps = _caps_client()
        await caps.fetch()
        app = _cockpit(chat_client=fake)
        async with app.run_test() as pilot:
            app._caps = caps
            inp = app.query_one("#agent", Input)
            inp.focus()
            inp.value = "/face sage"
            await pilot.pause()                        # Input.Changed → popup populated
            menu = app.query_one("#cmdmenu", OptionList)
            assert menu.display is True
            assert menu.get_option_at_index(0).id == "/face sage"
            await pilot.press("enter")
            await pilot.pause()
            assert fake.frames == [{"type": "switch_face", "face_id": "sage",
                                    "conversation_id": "conv-1"}]
            assert menu.display is False

    _run(scenario)


# ── Wave 1B Task 4: approvals actions — pane decide flow + in-chat prompts ──
def test_pane_approve_flow_confirms_then_decides():
    """a → typed-y confirm → POST /approvals/{id}/decide → outcome inline + pane refresh.
    The confirm step is the point: pressing ``a`` alone (or typing unrelated text, or
    answering ``n``) must NEVER reach the gateway."""
    from bobclaw_tui.pollers import Pollers

    async def scenario():
        session = _FakeSession([
            _Resp(200, {"id": "ap-1", "status": "approved", "decision": "recorded",
                        "agent_resume": "ok"}),          # the decide POST
            _Resp(200, {"faces": []}),                   # refresh: routing poll
            _Resp(200, {"items": []}),                   # refresh: approvals poll (empty)
            _Resp(200, {"items": []}),                   # refresh: agents poll (Wave 2 T3)
            _Resp(200, []),                              # refresh: faces poll (Wave 2 T3)
        ])
        app = _cockpit()
        async with app.run_test() as pilot:
            app._pollers = Pollers(app, "127.0.0.1:7836", "tok", session)
            app._approval_items = [
                {"id": "ap-1", "action_type": "email_send", "conversation_id": "cafebabe1234"},
            ]
            app._render_approvals_pane()

            await pilot.press("a")
            assert session.calls == []                    # keypress alone decides NOTHING
            assert app._pending_decision is not None
            lines = list(app.query_one("#council", Log).lines)
            assert any(ln.startswith("confirm: approve email_send") for ln in lines)

            # unrelated text while the prompt is open: swallowed, not a turn, not a decision
            await app.on_input_submitted(SimpleNamespace(value="hello"))
            assert session.calls == []
            assert app._pending_decision is not None

            # n cancels without deciding
            await app.on_input_submitted(SimpleNamespace(value="n"))
            assert session.calls == []
            assert app._pending_decision is None
            lines = list(app.query_one("#council", Log).lines)
            assert "approval cancelled — no decision recorded" in lines

            # a + y → the decide POST fires, the outcome renders inline, the pane refreshes
            # (the confirm focused the input line; blur so the keybind, not the Input, sees "a")
            app.query_one("#agent", Input).blur()
            await pilot.press("a")
            await app.on_input_submitted(SimpleNamespace(value="y"))
            method, url, kw = session.calls[0]
            assert method == "POST"
            assert url.endswith("/approvals/ap-1/decide")
            assert kw["json"] == {"decision": "approve"}
            lines = list(app.query_one("#council", Log).lines)
            assert "approval approved · agent resumed" in lines
            assert app._approval_items == []              # the decided row left the pane
            pane = str(app.query_one("#approvals", Static).content)
            assert "no pending approvals" in pane

    _run(scenario)


def test_pane_deny_decides_selected_row_and_surfaces_resume_failure():
    """d + y sends decision=reject for the SELECTED (j/k-moved) row; an agent_resume
    failure is rendered, not swallowed (the decision is still recorded gateway-side)."""
    from bobclaw_tui.pollers import Pollers

    async def scenario():
        session = _FakeSession([
            _Resp(200, {"id": "ap-2", "status": "rejected", "decision": "recorded",
                        "agent_resume": "failed", "agent_resume_message": "core unreachable"}),
            _Resp(200, {"faces": []}),
            _Resp(200, {"items": [{"id": "ap-1", "action_type": "email_send",
                                   "conversation_id": "cafebabe"}]}),
            _Resp(200, {"items": []}),                   # agents poll (Wave 2 T3)
            _Resp(200, []),                              # faces poll (Wave 2 T3)
        ])
        app = _cockpit()
        async with app.run_test() as pilot:
            app._pollers = Pollers(app, "127.0.0.1:7836", "tok", session)
            app._approval_items = [
                {"id": "ap-1", "action_type": "email_send", "conversation_id": "cafebabe"},
                {"id": "ap-2", "action_type": "cc_edit", "conversation_id": "beef"},
            ]
            app._render_approvals_pane()

            await pilot.press("j")                        # move the selection to row 2
            assert app._approval_sel == 1
            pane = str(app.query_one("#approvals", Static).content)
            assert "> cc_edit" in pane
            await pilot.press("d")
            await app.on_input_submitted(SimpleNamespace(value="y"))
            method, url, kw = session.calls[0]
            assert url.endswith("/approvals/ap-2/decide")  # the selected row, not row 0
            assert kw["json"] == {"decision": "reject"}
            lines = list(app.query_one("#council", Log).lines)
            assert "approval rejected · agent resume FAILED: core unreachable" in lines

    _run(scenario)


def test_approval_hotkeys_fire_while_input_focused():
    """Focus-trap fix (2026-08-21): the plain j/k/a/d letters are chat text while
    the message input has focus (the default state — the trap hit Travis twice in
    one session). The ctrl variants must drive the gate from that state."""
    from bobclaw_tui.pollers import Pollers

    async def scenario():
        session = _FakeSession([
            _Resp(200, {"id": "ap-1", "status": "rejected", "decision": "recorded",
                        "agent_resume": "rejected"}),   # the decide POST
            _Resp(200, {"faces": []}),                   # refresh: routing poll
            _Resp(200, {"items": []}),                   # refresh: approvals poll
            _Resp(200, {"items": []}),                   # refresh: agents poll
            _Resp(200, []),                              # refresh: faces poll
        ])
        app = _cockpit()
        async with app.run_test() as pilot:
            app._pollers = Pollers(app, "127.0.0.1:7836", "tok", session)
            app._approval_items = [
                {"id": "ap-0", "action_type": "email_send", "conversation_id": "aaaa"},
                {"id": "ap-1", "action_type": "cc_edit", "conversation_id": "bbbb"},
            ]
            app._render_approvals_pane()

            # the trap state: message input focused, half-typed text in it
            inp = app.query_one("#agent", Input)
            inp.focus()
            inp.value = "half-typed message"

            # a plain letter is typed text, never a gate key
            await pilot.press("d")
            assert app._pending_decision is None

            # ctrl variants: move the selection, open the deny confirm
            await pilot.press("ctrl+down")
            assert app._approval_sel == 1
            await pilot.press("ctrl+n")
            assert app._pending_decision is not None
            assert app._pending_decision["decision"] == "reject"
            assert app._pending_decision["item"]["id"] == "ap-1"

            # typed y fires the decide POST for the selected row
            await app.on_input_submitted(SimpleNamespace(value="y"))
            method, url, kw = session.calls[0]
            assert method == "POST"
            assert url.endswith("/approvals/ap-1/decide")
            assert kw["json"] == {"decision": "reject"}

    _run(scenario)


class _FakeApprovalChat:
    """Fake chat client whose turn emits one approval_request mid-stream (like the real
    gateway when core parks a gated action), and records the y/n answers."""

    def __init__(self, frame):
        self._frame = frame
        self.conv = "conv-1"
        self.answers = []
        self.turns = 0

    async def ensure_conversation(self, on_error=None):
        return self.conv

    async def stream_turn(self, prompt, *, council, profile, conversation_id, on_chunk,
                          on_error, on_approval=None, on_stopped=None):
        self.turns += 1
        on_chunk("working… ")
        if on_approval is not None:
            on_approval(self._frame)

    async def answer_approval(self, approval_id, approved):
        self.answers.append((approval_id, approved))


_APPROVAL_FRAME = {"type": "approval_request", "approval_id": "abc123",
                   "action": "email_send", "details": {"to": "travis@x.z"}}


async def _open_prompt(app, fake, pilot):
    """Run one turn whose stream surfaces the approval_request; return when the prompt
    is open in the agent log."""
    await app.on_input_submitted(SimpleNamespace(value="send the mail"))
    await app._turn_worker.wait()
    assert app._pending_decision == {"kind": "chat", "approval_id": "abc123"}
    lines = list(app.query_one("#council", Log).lines)
    assert any(ln.startswith("⚠ approval requested: email_send") for ln in lines)
    assert any("to=travis@x.z" in ln for ln in lines)


def test_in_chat_approval_prompt_y_sends_response():
    """The approval_request renders as a y/n prompt; while it's open, ordinary text is
    NOT sent as a turn; y sends the approval_response (approve)."""
    async def scenario():
        fake = _FakeApprovalChat(_APPROVAL_FRAME)
        app = _cockpit(chat_client=fake)
        async with app.run_test() as pilot:
            await _open_prompt(app, fake, pilot)
            await app.on_input_submitted(SimpleNamespace(value="some other prompt"))
            assert fake.turns == 1                        # swallowed — NOT a second turn
            assert fake.answers == []
            assert app._pending_decision is not None      # prompt still open
            await app.on_input_submitted(SimpleNamespace(value="y"))
            assert fake.answers == [("abc123", True)]
            assert app._pending_decision is None
            lines = list(app.query_one("#council", Log).lines)
            assert "approval answered: approved" in lines

    _run(scenario)


def test_in_chat_approval_prompt_n_sends_reject():
    async def scenario():
        fake = _FakeApprovalChat(_APPROVAL_FRAME)
        app = _cockpit(chat_client=fake)
        async with app.run_test() as pilot:
            await _open_prompt(app, fake, pilot)
            await app.on_input_submitted(SimpleNamespace(value="n"))
            assert fake.answers == [("abc123", False)]
            lines = list(app.query_one("#council", Log).lines)
            assert "approval answered: rejected" in lines

    _run(scenario)


def test_in_chat_approval_esc_dismisses_without_deciding():
    """Esc = NO decision: the prompt closes, nothing is sent (the approval stays pending
    for the pane to decide later)."""
    async def scenario():
        fake = _FakeApprovalChat(_APPROVAL_FRAME)
        app = _cockpit(chat_client=fake)
        async with app.run_test() as pilot:
            await _open_prompt(app, fake, pilot)
            await pilot.press("escape")
            assert app._pending_decision is None
            assert fake.answers == []
            lines = list(app.query_one("#council", Log).lines)
            assert "approval prompt dismissed — no decision recorded" in lines

    _run(scenario)


# ── Wave 1B Task 5: busy-turn queue + interrupt ──
class _FakeBusyChat:
    """A turn that streams until released — exercises the busy-turn queue (Task 5).

    The first turn blocks on ``release`` (a long stream); with ``auto`` set, later turns
    complete immediately (the FIFO drain test). ``stop_generation`` records the exact frame
    and emulates the gateway answering ``generation_stopped`` on the turn stream.
    """

    def __init__(self):
        self.conv = "conv-1"
        self.turns = []              # prompts, in send order
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.auto = False            # True ⇒ turns complete without waiting on release
        self.stops = []              # stop_generation frames (as sent)
        self._on_stopped = None
        self._stopped = False

    async def ensure_conversation(self, on_error=None):
        return self.conv

    async def stream_turn(self, prompt, *, council, profile, conversation_id, on_chunk,
                          on_error, on_approval=None, on_stopped=None):
        self.turns.append(prompt)
        self._on_stopped = on_stopped
        self._stopped = False
        if not self.auto:
            self.release.clear()
            self.started.set()
            await self.release.wait()
        else:
            self.started.set()
        if not self._stopped:
            on_chunk(f"reply:{prompt}")

    async def stop_generation(self, *, conversation_id):
        self.stops.append({"type": "stop_generation", "conversation_id": conversation_id})
        # the gateway answers stop_generation with generation_stopped on the turn stream
        self._stopped = True
        if self._on_stopped is not None:
            self._on_stopped({"type": "generation_stopped", "code": "stopped"})
        self.release.set()


def test_input_queues_while_turn_streams():
    """While a turn streams, a submitted message does NOT start a concurrent turn — it
    queues (status row shows the count, the queued text is logged) and drains when the
    stream ends."""
    async def scenario():
        fake = _FakeBusyChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="first"))
            await fake.started.wait()                  # the turn worker is streaming
            assert app._turn_busy()

            await app.on_input_submitted(SimpleNamespace(value="second"))
            assert fake.turns == ["first"]             # queued, NOT a concurrent turn
            assert list(app._queue) == [("second", False)]
            lines = list(app.query_one("#council", Log).lines)
            assert "queued (1): second" in lines       # the queued text is visible
            assert "queued: 1" in app._status_text     # the status row shows the count

            fake.auto = True                         # the drained turn completes at once
            fake.release.set()                       # stream ends → the queue drains
            await app._turn_worker.wait()              # one worker covers the whole drain
            assert fake.turns == ["first", "second"]   # sent as its own turn
            assert app._queue == []
            lines = list(app.query_one("#council", Log).lines)
            assert "you› second" in lines
            assert "bob› reply:second" in lines

    _run(scenario)


def test_queue_drains_fifo_on_complete():
    """Two queued messages drain in FIFO order, each as its own turn."""
    async def scenario():
        fake = _FakeBusyChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="first"))
            await fake.started.wait()
            await app.on_input_submitted(SimpleNamespace(value="second"))
            await app.on_input_submitted(SimpleNamespace(value="third"))
            assert fake.turns == ["first"]
            assert [p for p, _ in app._queue] == ["second", "third"]
            assert "queued: 2" in app._status_text

            fake.auto = True                           # drained turns complete at once
            fake.release.set()
            await app._turn_worker.wait()
            assert fake.turns == ["first", "second", "third"]   # FIFO, one turn each
            assert app._queue == []
            lines = list(app.query_one("#council", Log).lines)
            assert lines.index("you› second") < lines.index("you› third")

    _run(scenario)


def test_stop_sends_conversation_id_and_does_not_drain_queue():
    """/stop while busy sends stop_generation WITH conversation_id; generation_stopped
    renders inline; the queue is NOT auto-drained into the stopped turn."""
    async def scenario():
        fake = _FakeBusyChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="first"))
            await fake.started.wait()
            await app.on_input_submitted(SimpleNamespace(value="second"))   # queued

            await app.on_input_submitted(SimpleNamespace(value="/stop"))
            assert fake.stops == [{"type": "stop_generation",
                                   "conversation_id": "conv-1"}]
            await app._turn_worker.wait()
            lines = list(app.query_one("#council", Log).lines)
            assert any("[stopped]" in ln for ln in lines)        # generation_stopped rendered
            assert fake.turns == ["first"]                       # no drain into a stopped turn
            assert [p for p, _ in app._queue] == ["second"]      # queue stays queued

            # the next submitted turn runs, and its natural completion drains the queue
            fake.auto = True                                     # later turns complete at once
            await app.on_input_submitted(SimpleNamespace(value="third"))
            await app._turn_worker.wait()
            assert fake.turns == ["first", "third", "second"]
            assert app._queue == []

    _run(scenario)


def test_double_enter_while_busy_stops_turn():
    """Enter on an empty line while busy: the first warns, the second stops (/stop path)."""
    async def scenario():
        fake = _FakeBusyChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="first"))
            await fake.started.wait()
            await app.on_input_submitted(SimpleNamespace(value=""))
            assert fake.stops == []                              # one Enter only warns
            await app.on_input_submitted(SimpleNamespace(value=""))
            assert fake.stops == [{"type": "stop_generation",
                                   "conversation_id": "conv-1"}]
            await app._turn_worker.wait()
            lines = list(app.query_one("#council", Log).lines)
            assert any("[stopped]" in ln for ln in lines)

    _run(scenario)


def test_esc_clears_queue():
    """Esc with no approval prompt open CLEARS the busy-turn queue (with a log note)."""
    async def scenario():
        fake = _FakeBusyChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test() as pilot:
            await app.on_input_submitted(SimpleNamespace(value="first"))
            await fake.started.wait()
            await app.on_input_submitted(SimpleNamespace(value="second"))
            await app.on_input_submitted(SimpleNamespace(value="third"))
            assert len(app._queue) == 2

            await pilot.press("escape")
            assert app._queue == []
            lines = list(app.query_one("#council", Log).lines)
            assert "queue cleared (2 dropped)" in lines
            assert "queued:" not in app._status_text

            fake.release.set()                                   # nothing left to drain
            await app._turn_worker.wait()
            assert fake.turns == ["first"]

    _run(scenario)


class _FakeStreamWS:
    """A /ws/chat socket that holds the stream open until stop_generation arrives, then
    replays the gateway's generation_stopped answer. Records every sent frame."""

    def __init__(self):
        self.sent = []
        self._gate = asyncio.Event()
        self._tail = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_json(self, frame):
        self.sent.append(frame)
        if frame.get("type") == "stop_generation":
            self._tail.append({"type": "generation_stopped", "code": "stopped"})
            self._gate.set()

    def __aiter__(self):
        import aiohttp
        import json as _json

        async def gen():
            await self._gate.wait()
            for f in self._tail:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=_json.dumps(f))

        return gen()


def test_stop_generation_carries_conversation_id_on_wire():
    """Wire-level: ChatClient.stop_generation sends the exact frame (conversation_id
    REQUIRED post-1A) on the LIVE turn socket, and the generation_stopped reply surfaces
    via on_stopped."""
    from bobclaw_tui.chat import ChatClient

    async def scenario():
        ws = _FakeStreamWS()
        chat = ChatClient("127.0.0.1:7836", "tok", _FakeWSSession(ws),
                          state_path="/nonexistent/tui-state.json")
        stopped = []
        turn = asyncio.create_task(chat.stream_turn(
            "hi", council=False, profile=None, conversation_id="conv-1",
            on_chunk=lambda t: None, on_error=lambda m: None, on_stopped=stopped.append))
        for _ in range(100):                                     # let the turn frame send
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        await chat.stop_generation(conversation_id="conv-1")
        await asyncio.wait_for(turn, 2)
        assert ws.sent == [
            {"type": "message", "conversation_id": "conv-1", "content": "hi"},
            {"type": "stop_generation", "conversation_id": "conv-1"},
        ]
        assert stopped == [{"type": "generation_stopped", "code": "stopped"}]

    _run(scenario)


# ── Wave 2 Task 3: the Bots pane (teammate roster + select-to-bind + watermark) ──
class _FakeBotChat:
    """_FakeConvo-style fake with the watermark surface the Bots pane drives
    (``bot_last_seen`` / ``mark_bot_seen`` — the real ones live on ChatClient and share
    the ``tui-state.json`` state file)."""

    def __init__(self, seen=None):
        self.conv_id = "conv-0"
        self.title = "inbox"
        self.seen = dict(seen or {})
        self.seen_calls = []
        self.history_msgs = [
            {"role": "user", "content": "hi bot"},
            {"role": "assistant", "content": "hello human"},
        ]

    @property
    def conversation_id(self):
        return self.conv_id

    @property
    def conversation_title(self):
        return self.title

    async def ensure_conversation(self, on_error=None):
        return self.conv_id

    def bot_last_seen(self):
        return dict(self.seen)

    def mark_bot_seen(self, slug, seen_at):
        self.seen_calls.append((slug, seen_at))
        self.seen[slug] = seen_at

    async def use_conversation(self, conv):
        self.conv_id = str(conv["id"])
        self.title = conv.get("title")
        return self.conv_id

    async def history(self):
        return list(self.history_msgs)


_BOT_AGENTS = {"items": [
    {"slug": "helper", "display_name": "Assistant", "face_id": "assistant",
     "avatar": "🤖", "conversation_id": "conv-b1", "updated_at": "2026-08-18T10:00:00"},
    {"slug": "review", "display_name": "Reviewer", "face_id": "reviewer",
     "avatar": "🧐", "conversation_id": "conv-b2", "updated_at": "2026-08-18T09:00:00"},
    {"slug": "drone", "display_name": "Drone", "face_id": "worker-deepseek",
     "avatar": "🔧", "conversation_id": "conv-b3", "updated_at": "2026-08-18T08:00:00"},
]}
_BOT_FACES = [
    {"id": "assistant", "simple_slot": "quick"},   # teammate via simple_slot
    {"id": "reviewer", "bot": True},               # teammate via bot: true
    {"id": "worker-deepseek"},                     # NOT a teammate
]


def test_bots_pane_roster_renders_with_unread_watermark():
    """A panel refresh polls /agents + /faces and fills the Bots OptionList with the
    teammate bindings (non-teammate faces filtered); a binding whose activity is newer
    than its state-file watermark renders the ● unread mark."""
    from bobclaw_tui.pollers import Pollers

    async def scenario():
        session = _FakeSession([
            _Resp(200, {"faces": []}),      # routing poll
            _Resp(200, {"items": []}),      # approvals poll
            _Resp(200, _BOT_AGENTS),        # agents poll
            _Resp(200, _BOT_FACES),         # faces poll (a bare list)
        ])
        fake = _FakeBotChat(seen={"helper": "2026-08-18T09:30:00",
                                  "review": "2026-08-18T09:30:00"})
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            app._pollers = Pollers(app, "127.0.0.1:7836", "tok", session)
            await app._pollers.refresh_panels()
            pane = app.query_one("#bots", OptionList)
            ids = [pane.get_option_at_index(i).id for i in range(pane.option_count)]
            assert ids == ["helper", "review"]        # drone's face is not a teammate
            rows = [str(pane.get_option_at_index(i).prompt) for i in range(pane.option_count)]
            assert rows[0].startswith("●")            # 10:00 activity > 09:30 watermark
            assert "🤖 Assistant (helper) · 2026-08-18T10:00" in rows[0]
            assert rows[1].startswith(" ")            # 09:00 activity ≤ watermark
            assert "🧐 Reviewer (review)" in rows[1]
            assert [b["slug"] for b in app._bot_items] == ["helper", "review"]

    _run(scenario)


def test_bots_pane_select_binds_conversation_and_renders_history():
    """Selecting a bot binds its canonical conversation (the 1B-1 use_conversation +
    history path) and advances the unread watermark to the activity just opened."""
    async def scenario():
        fake = _FakeBotChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            app._bot_items = [_BOT_AGENTS["items"][0]]
            await app.on_option_list_option_selected(SimpleNamespace(
                option_list=SimpleNamespace(id="bots"),
                option=SimpleNamespace(id="helper"),
            ))
            assert fake.conversation_id == "conv-b1"          # bound the canonical chat
            assert fake.seen_calls == [("helper", "2026-08-18T10:00:00")]  # watermark
            assert "chat: Assistant" in app._status_text
            lines = list(app.query_one("#council", Log).lines)
            assert "— Assistant —" in lines
            assert "you› hi bot" in lines
            assert "bob› hello human" in lines

    _run(scenario)


def test_bots_pane_select_unknown_or_unbound_is_noop():
    """A select on a stale slug (or a message row with no binding) binds NOTHING and
    renders no history — the pane never navigates away from the active chat by accident."""
    async def scenario():
        fake = _FakeBotChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            app._bot_items = []
            await app.on_option_list_option_selected(SimpleNamespace(
                option_list=SimpleNamespace(id="bots"),
                option=SimpleNamespace(id="ghost"),
            ))
            assert fake.conversation_id == "conv-0"
            assert fake.seen_calls == []
            assert list(app.query_one("#council", Log).lines) == []
            # a binding WITHOUT a canonical conversation is not selectable either
            app._bot_items = [{"slug": "helper", "display_name": "Assistant",
                               "face_id": "assistant", "conversation_id": None,
                               "updated_at": "2026-08-18T10:00:00"}]
            await app.on_option_list_option_selected(SimpleNamespace(
                option_list=SimpleNamespace(id="bots"),
                option=SimpleNamespace(id="helper"),
            ))
            assert fake.conversation_id == "conv-0"
            assert fake.seen_calls == []

    _run(scenario)


# ── Wave 2 Task 4: the /bot command (resolve-or-create + bind + optional turn) ──
class _FakeBotCmdChat(_FakeBotChat):
    """_FakeBotChat + the ``/bot`` command surface: ``open_bot`` resolve-or-create
    (bindings = existing, creatable = slugs a POST /agents would mint) and a
    ``stream_turn`` that records ``(prompt, conversation_id)``."""

    def __init__(self, bindings=None, creatable=(), **kw):
        super().__init__(**kw)
        self.bindings = dict(bindings or {})
        self.creatable = set(creatable)
        self.open_calls = []
        self.turns = []

    async def open_bot(self, slug):
        self.open_calls.append(slug)
        if slug in self.bindings:
            return dict(self.bindings[slug])
        if slug in self.creatable:  # first use: POST /agents creates the binding
            binding = {"slug": slug, "display_name": slug.title(), "face_id": slug,
                       "conversation_id": f"conv-{slug}", "updated_at": "2026-08-18T11:00:00"}
            self.bindings[slug] = binding
            return dict(binding)
        return None  # no binding AND not a teammate face

    async def stream_turn(self, prompt, *, council, profile, conversation_id, on_chunk, on_error,
                          on_approval=None, on_stopped=None):
        self.turns.append((prompt, conversation_id))
        on_chunk("bot reply")


def test_bot_command_switches_to_existing_bot():
    """``/bot <slug>`` binds the bot's canonical conversation (the same
    use_conversation + history path as a pane select) and sends NO turn."""
    async def scenario():
        fake = _FakeBotCmdChat(bindings={"helper": _BOT_AGENTS["items"][0]})
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/bot helper"))
            assert fake.open_calls == ["helper"]
            assert fake.conversation_id == "conv-b1"        # bound the canonical chat
            assert fake.seen_calls == [("helper", "2026-08-18T10:00:00")]  # watermark
            assert "chat: Assistant" in app._status_text
            assert fake.turns == []                          # navigation only
            lines = list(app.query_one("#council", Log).lines)
            assert "— Assistant —" in lines
            assert "you› hi bot" in lines
            assert "bob› hello human" in lines

    _run(scenario)


def test_bot_command_first_use_creates_then_binds():
    """First use of a teammate slug: open_bot creates the binding, and the command
    binds the CREATED canonical conversation."""
    async def scenario():
        fake = _FakeBotCmdChat(creatable={"helper"})
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/bot helper"))
            assert fake.open_calls == ["helper"]
            assert fake.conversation_id == "conv-helper"    # the created binding's chat
            assert "chat: Helper" in app._status_text
            lines = list(app.query_one("#council", Log).lines)
            assert "— Helper —" in lines

    _run(scenario)


def test_bot_command_with_message_sends_a_normal_turn():
    """``/bot <slug> <message>`` switches AND sends the message as a normal
    ``_send_chat`` turn on the bot's conversation (no second dispatch engine)."""
    async def scenario():
        fake = _FakeBotCmdChat(bindings={"helper": _BOT_AGENTS["items"][0]})
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/bot helper hello there"))
            await app._turn_worker.wait()  # turns stream in a worker (Wave 1B Task 4)
            assert fake.conversation_id == "conv-b1"
            assert fake.turns == [("hello there", "conv-b1")]  # one turn, bot's conv
            lines = list(app.query_one("#council", Log).lines)
            assert "you› hello there" in lines
            assert any(ln == "bob› bot reply" for ln in lines)

    _run(scenario)


def test_bot_command_unknown_slug_is_noop():
    """An unknown slug that is NOT a teammate face: a clear log line, no binding, no
    watermark, no history, no turn — the active conversation is untouched."""
    async def scenario():
        fake = _FakeBotCmdChat()
        app = _cockpit(chat_client=fake)
        async with app.run_test():
            await app.on_input_submitted(SimpleNamespace(value="/bot ghost"))
            assert fake.open_calls == ["ghost"]
            assert fake.conversation_id == "conv-0"         # unchanged
            assert fake.seen_calls == [] and fake.turns == []
            lines = list(app.query_one("#council", Log).lines)
            assert any("unknown bot: ghost" in ln for ln in lines)
            assert "— inbox —" not in lines                 # no history render

    _run(scenario)


def test_bots_command_focuses_bots_pane():
    """``/bots`` focuses the Bots pane (consistency with ``/chats`` opening its picker)."""
    async def scenario():
        app = _cockpit(chat_client=_FakeBotChat())
        async with app.run_test() as pilot:
            await app.on_input_submitted(SimpleNamespace(value="/bots"))
            await pilot.pause()  # focus lands via Textual's posted focus message
            assert app.query_one("#bots", OptionList).has_focus

    _run(scenario)


def test_bot_arg_completions_from_roster():
    """``/bot`` completes slugs from the polled Bots-pane roster (option id = the full
    line, label = slug + display name); empty past the slug token."""
    from bobclaw_tui.commands import bot_arg_completions

    rows = bot_arg_completions(_BOT_AGENTS["items"], "/bot h")
    assert [oid for oid, _ in rows] == ["/bot helper"]
    assert "Assistant" in rows[0][1]
    assert [oid for oid, _ in bot_arg_completions(_BOT_AGENTS["items"], "/bot ")] == [
        "/bot helper", "/bot review", "/bot drone"]
    assert bot_arg_completions(_BOT_AGENTS["items"], "/bot helper hello") == []  # past slug
    assert bot_arg_completions(_BOT_AGENTS["items"], "/chats") == []
    assert bot_arg_completions([], "/bot h") == []


def test_open_bot_existing_binding_skips_create(tmp_path):
    """ChatClient.open_bot wire path: GET /agents/{slug} 200 → the binding, NO POST."""
    from bobclaw_tui.chat import ChatClient

    async def scenario():
        binding = {"slug": "helper", "display_name": "Assistant", "face_id": "assistant",
                   "conversation_id": "conv-b1", "updated_at": "2026-08-18T10:00:00"}
        session = _FakeSession([_Resp(200, binding)])
        chat = ChatClient("127.0.0.1:7836", "tok", session,
                          state_path=tmp_path / "tui-state.json")
        assert await chat.open_bot("helper") == binding
        assert [m for m, _, _ in session.calls] == ["GET"]
        assert session.calls[0][1].endswith("/agents/helper")

    _run(scenario)


def test_open_bot_first_use_posts_agents(tmp_path):
    """ChatClient.open_bot wire path: GET 404 → the slug IS a teammate face
    (``simple_slot``) → POST /agents with face_id=slug + the face's display_name → the
    created binding."""
    from bobclaw_tui.chat import ChatClient

    async def scenario():
        session = _FakeSession([
            _Resp(404, {"error": "not found"}),                     # GET /agents/helper
            _Resp(200, [{"id": "helper", "display_name": "Helper",  # GET /faces
                         "simple_slot": "quick"}]),
            _Resp(201, {"slug": "helper", "display_name": "Helper",  # POST /agents
                        "face_id": "helper", "conversation_id": "conv-b9",
                        "updated_at": "2026-08-18T11:00:00"}),
        ])
        chat = ChatClient("127.0.0.1:7836", "tok", session,
                          state_path=tmp_path / "tui-state.json")
        binding = await chat.open_bot("helper")
        assert binding["conversation_id"] == "conv-b9"
        assert [m for m, _, _ in session.calls] == ["GET", "GET", "POST"]
        method, url, kw = session.calls[2]
        assert url.endswith("/agents")
        assert kw["json"] == {"slug": "helper", "face_id": "helper", "display_name": "Helper"}

    _run(scenario)


def test_open_bot_unknown_or_non_teammate_face_returns_none(tmp_path):
    """ChatClient.open_bot wire path: GET 404 + the slug is absent from /faces (or names
    a NON-teammate face) → None, and NO POST is attempted (fail-closed on creates)."""
    from bobclaw_tui.chat import ChatClient

    async def scenario():
        faces = [{"id": "helper", "simple_slot": "quick"}, {"id": "worker-deepseek"}]
        s1 = _FakeSession([_Resp(404, {}), _Resp(200, faces)])
        chat1 = ChatClient("127.0.0.1:7836", "tok", s1,
                           state_path=tmp_path / "s1.json")
        assert await chat1.open_bot("ghost") is None        # no face with that id
        assert [m for m, _, _ in s1.calls] == ["GET", "GET"]

        s2 = _FakeSession([_Resp(404, {}), _Resp(200, faces)])
        chat2 = ChatClient("127.0.0.1:7836", "tok", s2,
                           state_path=tmp_path / "s2.json")
        assert await chat2.open_bot("worker-deepseek") is None  # face, but not a teammate
        assert [m for m, _, _ in s2.calls] == ["GET", "GET"]

    _run(scenario)


# ── agent-log soft wrap (2026-08-21): Log never wraps, so wrapping happens at
# write time; _wrap_chunk is the pure seam — no Textual needed. ──

def test_wrap_chunk_short_text_unchanged():
    assert _wrap_chunk("hello", 20) == ("hello", 5)


def test_wrap_chunk_wraps_at_space():
    # "aa bb" fills the window exactly; the second space becomes the break
    assert _wrap_chunk("aa bb cc", 5) == ("aa bb\ncc", 2)


def test_wrap_chunk_hard_breaks_long_word():
    out, col = _wrap_chunk("x" * 12, 5)
    assert out == "xxxxx\nxxxxx\nxx"
    assert col == 2


def test_wrap_chunk_newline_resets_column():
    out, col = _wrap_chunk("ab\ncd", 10)
    assert out == "ab\ncd"
    assert col == 2


def test_wrap_chunk_carries_column_across_streamed_chunks():
    # streamed tokens arrive split mid-window: the second chunk must wrap as if
    # the line had been written in one piece
    first, col = _wrap_chunk("aa b", 5)
    assert (first, col) == ("aa b", 4)
    second, col = _wrap_chunk("b cc", 5, col)
    assert second == "b\ncc"  # 'bb' finishes the line, the space breaks, 'cc' wraps
    assert all(len(ln) <= 5 for ln in (first + second).split("\n"))


class _FakeLongStreamChat:
    """Streams a long reply in small chunks, like a real token stream."""

    def __init__(self, reply: str):
        self._reply = reply
        self.conv = "conv-1"

    async def ensure_conversation(self, on_error=None):
        return self.conv

    async def stream_turn(self, prompt, *, council, profile, conversation_id, on_chunk,
                          on_error, on_approval=None, on_stopped=None):
        for i in range(0, len(self._reply), 7):  # deliberately mid-word splits
            on_chunk(self._reply[i:i + 7])


def test_streamed_reply_wraps_to_pane_width():
    """The 2026-08-21 wrap fix, end to end: a long streamed reply must not run
    past the agent pane — every rendered line fits the inner width, and the
    text survives (rejoined lines contain the full reply words)."""
    reply = " ".join(f"word{i}" for i in range(60))  # ~420 chars, no newlines

    async def scenario():
        app = _cockpit(chat_client=_FakeLongStreamChat(reply))
        async with app.run_test() as pilot:
            await app.on_input_submitted(SimpleNamespace(value="tell me things"))
            await app._turn_worker.wait()
            log = app.query_one("#council", Log)
            inner = max(20, log.size.width - 2)
            lines = [str(ln) for ln in log.lines]
            assert any("word59" in ln for ln in lines)          # the end made it
            assert all(len(ln) <= inner for ln in lines), (
                [ln for ln in lines if len(ln) > inner])
            rejoined = " ".join(lines)
            for token in ("word0", "word30", "word59"):
                assert token in rejoined

    _run(scenario)
