"""Tests for the CDP surface adapter with the CDP socket + Chrome MOCKED.

Zero network, zero Chrome: a ``FakeCdpClient`` is the only transport. Pins the MS2-G5
contract (CONTRACTS-G5.md): capture builds a Frame from the AX tree + screenshot hash;
act maps every ActionKind to the right CDP Input call and is total/fail-safe; the module
is import-light (the real websocket client is lazy); CdpSurface conforms to the Surface ABC.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from core.gui.framediff import hash_bytes
from core.gui.surface import Surface
from core.gui.surfaces.cdp import CdpError, CdpSurface
from core.gui.types import A11yNode, Action, ActionKind, ActionResult, Frame


class FakeCdpClient:
    """Test double implementing the CDP client protocol.

    Records every call to *call()* and returns scripted responses keyed by method.
    Default PNG data: b"PNGDATA". Default viewport: (1000, 800).
    """

    def __init__(
        self,
        png: bytes = b"PNGDATA",
        ax_nodes: list[dict[str, Any]] | None = None,
        boxes: dict[int, list[int]] | None = None,
        viewport: tuple[int, int] = (1000, 800),
        raise_on: set[str] | None = None,
    ) -> None:
        self.png = png
        self.ax_nodes = ax_nodes if ax_nodes is not None else []
        self.boxes = boxes if boxes is not None else {}
        self.viewport = viewport
        self.raise_on = raise_on if raise_on is not None else set()
        self.calls: list[tuple[str, dict | None]] = []

    def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        self.calls.append((method, params))
        if method in self.raise_on:
            raise CdpError(method, "forced error")

        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self.png).decode()}
        if method == "Page.getLayoutMetrics":
            return {
                "cssLayoutViewport": {
                    "clientWidth": self.viewport[0],
                    "clientHeight": self.viewport[1],
                }
            }
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self.ax_nodes}
        if method == "DOM.getBoxModel":
            bid = params.get("backendNodeId") if params else None
            content = self.boxes[bid]  # raises KeyError if absent
            return {"model": {"content": content}}
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        # Input.*, Page/DOM/Accessibility.enable, etc.
        return {}

    def close(self) -> None:
        pass


class TestCapture:
    """Tests for CdpSurface.capture()."""

    def test_capture_builds_frame_from_axtree(self) -> None:
        """Capture returns a Frame with correct a11y, size, image_hash, and incremented seq."""
        # Two nodes: button (bid 7) and status (bid 8)
        ax_nodes: list[dict] = [
            {
                "role": {"value": "button"},
                "name": {"value": "Add item"},
                "value": {"value": ""},
                "backendDOMNodeId": 7,
            },
            {
                "role": {"value": "status"},
                "name": {"value": "Status"},
                "value": {"value": ""},
                "backendDOMNodeId": 8,
            },
        ]
        boxes: dict[int, list[int]] = {
            7: [10, 20, 90, 20, 90, 50, 10, 50],
            8: [8, 100, 976, 100, 976, 118, 8, 118],
        }
        fake = FakeCdpClient(ax_nodes=ax_nodes, boxes=boxes, viewport=(1000, 800))
        surface = CdpSurface(fake)

        frame1 = surface.capture()
        assert frame1.seq == 1
        assert frame1.size == (1000, 800)
        assert frame1.image_hash == hash_bytes(b"PNGDATA")

        # Two a11y nodes
        assert len(frame1.a11y) == 2

        # Find the button
        btn = next(n for n in frame1.a11y if n.role == "button")
        assert btn.name == "Add item"
        assert btn.node_id == "7"
        # Expected bounds (10,20,80,30)
        assert btn.bounds == (10, 20, 80, 30)

        # Find the status
        st = next(n for n in frame1.a11y if n.role == "status")
        assert st.name == "Status"
        # Expected bounds (8,100,968,18)
        assert st.bounds == (8, 100, 968, 18)

        # Second capture must increase seq
        frame2 = surface.capture()
        assert frame2.seq == 2

    def test_capture_hashes_pixels_not_raw(self) -> None:
        """image_hash equals hash of decoded PNG; frame has no raw bytes attribute."""
        fake = FakeCdpClient(png=b"PNGDATA")
        surface = CdpSurface(fake)

        frame = surface.capture()
        # hash of decoded base64
        expected_hash = hash_bytes(base64.b64decode(base64.b64encode(b"PNGDATA")))
        assert frame.image_hash == expected_hash
        # no raw bytes or base64 string on the frame
        assert not hasattr(frame, "data")
        # verify no field equals the raw PNG bytes / base64
        for attr in ("image_hash", "size", "a11y", "seq"):
            val = getattr(frame, attr)
            if isinstance(val, bytes):
                assert val != b"PNGDATA"
            elif isinstance(val, str):
                assert val != base64.b64encode(b"PNGDATA").decode()
            elif isinstance(val, tuple):
                for item in val:
                    if isinstance(item, bytes):
                        assert item != b"PNGDATA"

        # Two identical screenshots give equal hash
        frame2 = surface.capture()
        assert frame2.image_hash == frame.image_hash

        # Change PNG data -> different hash
        fake.png = b"OTHERDATA"
        frame3 = surface.capture()
        assert frame3.image_hash != frame.image_hash

        # The SURFACE instance must not retain the raw bytes / base64 either (no-raw-pixel
        # rule applies to self, not just the Frame).
        raw = b"OTHERDATA"
        b64 = base64.b64encode(raw).decode()
        for v in vars(surface).values():
            assert v != raw
            assert v != b64

    def test_capture_skips_ignored_and_unboxable(self) -> None:
        """Ignores 'ignored' nodes; sets bounds=None when box model fails."""
        ax_nodes: list[dict] = [
            {
                "role": {"value": "button"},
                "name": {"value": "Good"},
                "value": {"value": ""},
                "backendDOMNodeId": 7,
            },
            {
                "ignored": True,
                "role": {"value": "hidden"},
                "name": {"value": "Ignored"},
                "value": {"value": ""},
                "backendDOMNodeId": 8,
            },
            {
                "role": {"value": "status"},
                "name": {"value": "Unboxable"},
                "value": {"value": ""},
                "backendDOMNodeId": 99,  # no box defined
            },
        ]
        boxes = {7: [10, 20, 90, 20, 90, 50, 10, 50]}  # only good node's box
        fake = FakeCdpClient(ax_nodes=ax_nodes, boxes=boxes)
        surface = CdpSurface(fake)

        # Should not raise
        frame = surface.capture()

        # Only two nodes present: the button and the unboxable node (ignored is dropped)
        assert len(frame.a11y) == 2

        # Button has correct bounds
        btn = next(n for n in frame.a11y if n.name == "Good")
        assert btn.bounds is not None
        assert btn.bounds == (10, 20, 80, 30)

        # Unboxable node has bounds=None (since box model raised)
        unboxable = next(n for n in frame.a11y if n.name == "Unboxable")
        assert unboxable.bounds is None

        # The ignored node is dropped entirely
        assert all(n.name != "Ignored" for n in frame.a11y)


class TestAct:
    """Tests for CdpSurface.act()."""

    def test_act_click_by_coord(self) -> None:
        """Click dispatches mousePressed and mouseReleased at the given coordinates."""
        fake = FakeCdpClient()
        surface = CdpSurface(fake)

        result = surface.act(Action(kind=ActionKind.CLICK, coord=(40, 35)))
        assert result.performed is True
        assert result.error == ""

        # Expect two dispatchMouseEvent calls
        mouse_calls = [
            (m, p)
            for m, p in fake.calls
            if m == "Input.dispatchMouseEvent"
        ]
        assert len(mouse_calls) == 2
        # First call: mousePressed
        _, params1 = mouse_calls[0]
        assert params1["type"] == "mousePressed"
        assert params1["x"] == 40
        assert params1["y"] == 35
        assert params1["button"] == "left"
        # Second call: mouseReleased
        _, params2 = mouse_calls[1]
        assert params2["type"] == "mouseReleased"
        assert params2["x"] == 40
        assert params2["y"] == 35
        assert params2["button"] == "left"

    def test_act_click_by_target_resolves_center(self) -> None:
        """Click with target resolves node center and dispatches accordingly."""
        ax_nodes: list[dict] = [
            {
                "role": {"value": "button"},
                "name": {"value": "MyButton"},
                "value": {"value": ""},
                "backendDOMNodeId": 7,
            }
        ]
        boxes = {7: [10, 20, 90, 20, 90, 50, 10, 50]}  # center (50,35)
        fake = FakeCdpClient(ax_nodes=ax_nodes, boxes=boxes)
        surface = CdpSurface(fake)

        # Act with target = "7"
        result = surface.act(Action(kind=ActionKind.CLICK, target="7"))
        assert result.performed is True

        # Find the dispatchMouseEvent calls
        mouse_calls = [
            p
            for m, p in fake.calls
            if m == "Input.dispatchMouseEvent"
        ]
        assert len(mouse_calls) == 2
        assert mouse_calls[0]["x"] == 50
        assert mouse_calls[0]["y"] == 35

        # Unresolvable target
        fake2 = FakeCdpClient(ax_nodes=ax_nodes, boxes=boxes)
        surface2 = CdpSurface(fake2)
        result2 = surface2.act(Action(kind=ActionKind.CLICK, target="nope"))
        assert result2.performed is False
        assert result2.error != ""
        # No dispatch should have been made
        mouse_calls2 = [
            m for m, _ in fake2.calls if m.startswith("Input.dispatch")
        ]
        assert len(mouse_calls2) == 0

    def test_act_type_inserts_text(self) -> None:
        """Type action clicks to focus then calls Input.insertText."""
        ax_nodes: list[dict] = [
            {
                "role": {"value": "textbox"},
                "name": {"value": "Input field"},
                "value": {"value": ""},
                "backendDOMNodeId": 7,
            }
        ]
        boxes = {7: [100, 200, 300, 200, 300, 250, 100, 250]}  # center (200,225)
        fake = FakeCdpClient(ax_nodes=ax_nodes, boxes=boxes)
        surface = CdpSurface(fake)

        result = surface.act(
            Action(kind=ActionKind.TYPE, text="hello", target="7")
        )
        assert result.performed is True

        # Expect a click to focus (two mouse events) and then Input.insertText
        mouse_calls = [
            p
            for m, p in fake.calls
            if m == "Input.dispatchMouseEvent"
        ]
        assert len(mouse_calls) == 2
        # center (200,225)
        assert mouse_calls[0]["x"] == 200
        assert mouse_calls[0]["y"] == 225

        insert_calls = [
            p
            for m, p in fake.calls
            if m == "Input.insertText"
        ]
        assert len(insert_calls) == 1
        assert insert_calls[0]["text"] == "hello"

    def test_act_key_dispatches_keydown_keyup(self) -> None:
        """KEY action dispatches keyDown and keyUp."""
        fake = FakeCdpClient()
        surface = CdpSurface(fake)

        result = surface.act(Action(kind=ActionKind.KEY, key="Enter"))
        assert result.performed is True

        key_calls = [
            p
            for m, p in fake.calls
            if m == "Input.dispatchKeyEvent"
        ]
        assert len(key_calls) == 2
        assert key_calls[0]["type"] == "keyDown"
        assert key_calls[0]["key"] == "Enter"
        assert key_calls[1]["type"] == "keyUp"
        assert key_calls[1]["key"] == "Enter"

    def test_act_key_space_inserts_text(self) -> None:
        """audit r2: the printable Space key carries 'text' so keyDown inserts a space char."""
        fake = FakeCdpClient()
        surface = CdpSurface(fake)
        result = surface.act(Action(kind=ActionKind.KEY, key="Space"))
        assert result.performed is True
        key_calls = [p for m, p in fake.calls if m == "Input.dispatchKeyEvent"]
        assert len(key_calls) == 2
        assert key_calls[0]["key"] == " "
        assert key_calls[0].get("text") == " "  # printable -> text set, else nothing is typed

    def test_act_scroll_wheel(self) -> None:
        """SCROLL dispatches mouseWheel with appropriate deltaX/deltaY."""
        fake = FakeCdpClient()
        surface = CdpSurface(fake)

        # Down scroll
        result = surface.act(
            Action(
                kind=ActionKind.SCROLL,
                direction="down",
                amount=120,
                coord=(5, 5),
            )
        )
        assert result.performed is True
        wheel_calls = [
            p
            for m, p in fake.calls
            if m == "Input.dispatchMouseEvent" and p["type"] == "mouseWheel"
        ]
        assert len(wheel_calls) == 1
        assert wheel_calls[0]["deltaX"] == 0
        assert wheel_calls[0]["deltaY"] == 120  # down positive

        # Up scroll
        fake2 = FakeCdpClient()
        surface2 = CdpSurface(fake2)
        result2 = surface2.act(
            Action(
                kind=ActionKind.SCROLL,
                direction="up",
                amount=120,
                coord=(10, 10),
            )
        )
        assert result2.performed is True
        wheel_calls2 = [
            p
            for m, p in fake2.calls
            if m == "Input.dispatchMouseEvent" and p["type"] == "mouseWheel"
        ]
        assert len(wheel_calls2) == 1
        assert wheel_calls2[0]["deltaY"] == -120  # up negative

    def test_act_noop(self) -> None:
        """NOOP action does nothing and returns performed True."""
        fake = FakeCdpClient()
        surface = CdpSurface(fake)

        result = surface.act(Action(kind=ActionKind.NOOP))
        assert result.performed is True
        # No Input.* calls
        input_calls = [m for m, _ in fake.calls if m.startswith("Input.")]
        assert len(input_calls) == 0

    def test_act_click_target_unboxable_is_failsafe(self) -> None:
        """audit r4: a target node with bounds=None (unboxable) -> performed=False, no dispatch,
        no raise (act never computes a center from None)."""
        ax_nodes: list[dict] = [
            {
                "role": {"value": "button"},
                "name": {"value": "Ghost"},
                "value": {"value": ""},
                "backendDOMNodeId": 7,
            }
        ]
        fake = FakeCdpClient(ax_nodes=ax_nodes, boxes={})  # no box for bid 7 -> bounds None
        surface = CdpSurface(fake)
        result = surface.act(Action(kind=ActionKind.CLICK, target="7"))
        assert result.performed is False
        assert result.error != ""
        assert not any(m.startswith("Input.dispatch") for m, _ in fake.calls)

    def test_act_failsafe_on_internal_cdp_error(self) -> None:
        """audit r4: an error from an INTERNAL cdp call during act() (a CLICK-by-target captures
        first) is caught -> performed=False (the whole act() is total, not just the Input call)."""
        fake = FakeCdpClient(raise_on={"Accessibility.getFullAXTree"})
        surface = CdpSurface(fake)
        result = surface.act(Action(kind=ActionKind.CLICK, target="anything"))
        assert result.performed is False
        assert result.error != ""

    def test_act_total_failsafe(self) -> None:
        """act never raises for ANY action kind — a CDP error -> performed=False (audit r2)."""
        all_input = {
            "Input.dispatchMouseEvent",
            "Input.insertText",
            "Input.dispatchKeyEvent",
        }
        cases = [
            Action(kind=ActionKind.CLICK, coord=(1, 1)),
            Action(kind=ActionKind.TYPE, text="x"),  # no target -> insertText raises
            Action(kind=ActionKind.KEY, key="Enter"),
            Action(kind=ActionKind.SCROLL, direction="down", amount=10, coord=(1, 1)),
        ]
        for action in cases:
            fake = FakeCdpClient(raise_on=all_input)
            surface = CdpSurface(fake)
            result = surface.act(action)  # must NEVER raise out
            assert result.performed is False, action.kind
            assert result.error != "", action.kind


class TestPurityAndConformance:
    """Import purity and ABC conformance."""

    def test_import_purity(self) -> None:
        """Source contains no banned imports; importing module loads no banned modules."""
        bobclaw_core = Path(__file__).resolve().parents[3]
        # Read via the absolute base (not a cwd-relative path) so this is run-dir-robust.
        src = (bobclaw_core / "core" / "gui" / "surfaces" / "cdp.py").read_text(encoding="utf-8")
        for banned in ("core.backends", "core.nodes", "aiohttp", "requests", "httpx"):
            assert banned not in src

        # Subprocess probe: importing the module must NOT pull in websockets (the real
        # ws client is a lazy import inside connect()) nor any backend/node module.
        bobclaw_core = Path(__file__).resolve().parents[3]
        code = (
            "import core.gui.surfaces.cdp, sys; "
            "print(any(m == 'websockets' or m.startswith('websockets.') "
            "or m == 'core.backends' or m == 'core.nodes' for m in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(bobclaw_core),
            env={**os.environ, "PYTHONPATH": str(bobclaw_core)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"

    def test_connect_closes_client_on_enable_failure(self) -> None:
        """audit r1: if a domain-enable call fails, connect() closes the orphaned ws client."""

        class FakeWS:
            def __init__(self) -> None:
                self.closed = False
                self._last: dict = {}

            def send(self, msg: str) -> None:
                self._last = json.loads(msg)

            def recv(self, timeout: float | None = None) -> str:
                # Reply to whatever id was sent with an ERROR frame -> CdpError on the
                # first Page.enable, exercising the enable-failure path.
                return json.dumps({"id": self._last["id"], "error": {"message": "no"}})

            def close(self) -> None:
                self.closed = True

        fake_ws = FakeWS()
        with mock.patch(
            "core.gui.surfaces.cdp._discover_page_ws", return_value="ws://x/devtools/page/1"
        ), mock.patch("websockets.sync.client.connect", return_value=fake_ws):
            raised = False
            try:
                CdpSurface.connect(1234)
            except CdpError:
                raised = True
        assert raised is True
        assert fake_ws.closed is True  # the orphaned client was closed on the error path

    def test_launch_cleans_udd_on_popen_failure(self) -> None:
        """audit r1: if subprocess.Popen raises, launch() removes the throwaway user-data-dir."""
        created = tempfile.mkdtemp(prefix="g5test_udd_")
        assert os.path.isdir(created)
        with mock.patch(
            "core.gui.surfaces.cdp._resolve_chrome", return_value=r"C:/fake/chrome.exe"
        ), mock.patch(
            "core.gui.surfaces.cdp.tempfile.mkdtemp", return_value=created
        ), mock.patch(
            "core.gui.surfaces.cdp.subprocess.Popen", side_effect=OSError("boom")
        ):
            raised = False
            try:
                CdpSurface.launch(url="about:blank")
            except OSError:
                raised = True
        assert raised is True
        assert not os.path.isdir(created)  # the throwaway dir was cleaned up on the error path

    def test_transport_discards_stale_out_of_order(self) -> None:
        """audit r2: _WebSocketCdpClient discards a stale (id < current) response, never grows _pending."""
        from core.gui.surfaces.cdp import _WebSocketCdpClient

        class FakeWS:
            def __init__(self, frames: list[dict]) -> None:
                self.frames = list(frames)
                self.sent: list[dict] = []

            def send(self, msg: str) -> None:
                self.sent.append(json.loads(msg))

            def recv(self, timeout: float | None = None) -> str:
                return json.dumps(self.frames.pop(0))

            def close(self) -> None:
                pass

        # First call() uses id=1. Feed a STALE id=0 response (a late reply from a prior,
        # timed-out call) then the real id=1 reply: the stale one must be discarded.
        ws = FakeWS([{"id": 0, "result": {"stale": True}}, {"id": 1, "result": {"ok": True}}])
        client = _WebSocketCdpClient(ws)
        assert client.call("X") == {"ok": True}
        assert client._pending == {}  # the stale id=0 was discarded, not buffered

    def test_transport_skips_events(self) -> None:
        """audit r4: a CDP EVENT (a frame with no 'id') is skipped, never mistaken for the response."""
        from core.gui.surfaces.cdp import _WebSocketCdpClient

        class FakeWS:
            def __init__(self, frames: list[dict]) -> None:
                self.frames = list(frames)

            def send(self, msg: str) -> None:
                pass

            def recv(self, timeout: float | None = None) -> str:
                return json.dumps(self.frames.pop(0))

            def close(self) -> None:
                pass

        # An event (no "id") arrives first, then the real id=1 response.
        ws = FakeWS([{"method": "Page.loadEventFired", "params": {}}, {"id": 1, "result": {"ok": True}}])
        client = _WebSocketCdpClient(ws)
        assert client.call("X") == {"ok": True}  # the event was skipped

    def test_close_kills_by_pid_and_cleans_owned_udd(self) -> None:
        """audit r2: close() kills the owned process by its OWN pid (never a broad chrome kill)
        and removes the throwaway user-data-dir; it is idempotent."""
        created = tempfile.mkdtemp(prefix="g5test_close_")
        assert os.path.isdir(created)

        class FakeProc:
            def __init__(self) -> None:
                self.pid = 424242
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

        proc = FakeProc()
        fake = FakeCdpClient()
        surface = CdpSurface(fake, _process=proc, _user_data_dir=created, _owns_process=True)

        runs: list[tuple] = []
        with mock.patch(
            "core.gui.surfaces.cdp.subprocess.run",
            side_effect=lambda *a, **k: runs.append((a, k)),
        ):
            surface.close()

        assert proc.terminated is True
        assert not os.path.isdir(created)  # the throwaway dir was removed

        if sys.platform == "win32":
            cmd_text = " ".join(str(x) for a, _ in runs for x in (a[0] if a else []))
            assert "Stop-Process" in cmd_text
            assert "424242" in cmd_text          # by the EXACT pid
            assert "chrome" not in cmd_text.lower()  # NEVER a broad chrome kill

        # Idempotent: a second close() does not raise.
        surface.close()

    def test_abc_conformance(self) -> None:
        """CdpSurface conforms to the Surface abstract class."""
        assert issubclass(CdpSurface, Surface)
        fake = FakeCdpClient()
        surface = CdpSurface(fake)
        assert isinstance(surface, Surface)

        # reset() is a no-op on the CDP surface (does NOT navigate)
        fake.calls.clear()
        ret = surface.reset()
        assert ret is None
        assert len(fake.calls) == 0
