"""CDP/browser Surface adapter for BoBClaw MS2-G5.

Implements :class:`core.gui.surface.Surface` by driving Chrome over the Chrome
DevTools Protocol (CDP) via a JSON-RPC websocket transport.

DESIGN-MS-D1 §3-G5 / OD#5 — CDP/browser-first adapter (Windows native CU pipes are
upstream-broken per ``[[codex-cua-windows-gotchas]]``; the browser/CDP control path is the
reliable one).  Real I/O lives behind the ABC so the loop + the 77 skeleton tests stay
headless (``FakeSurface`` is the test double).  The desktop / UIA adapter is explicitly
DEFERRED to a later, higher-risk sprint.

The real websocket client (``websockets.sync``) is a LAZY import inside :meth:`CdpSurface.connect`,
so importing this module pulls in no websocket/chrome dependency and the unit tests (which inject a
fake CDP client) run with zero network and zero Chrome.  No model call is ever made.

Usage::

    # Launch a fresh headless Chrome and connect:
    surface = CdpSurface.launch(url="about:blank")
    frame = surface.capture()
    result = surface.act(Action(kind=ActionKind.CLICK, coord=(100, 200)))
    surface.close()

    # Or inject a fake client for testing:
    surface = CdpSurface(FakeCdpClient(...))
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterable
from typing import Optional, Protocol, Tuple, cast

from core.gui.framediff import hash_bytes
from core.gui.surface import Surface
from core.gui.types import A11yNode, Action, ActionKind, ActionResult, Frame

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

class CdpError(RuntimeError):
    """Raised when a CDP call returns an error frame.

    Attributes:
        method: The CDP method that failed.
        payload: The raw error payload from the CDP response.
    """

    def __init__(self, method: str, payload: object) -> None:
        self.method = method
        self.payload = payload
        msg = f"CDP error on {method!r}: {payload}"
        super().__init__(msg)


class CdpClient(Protocol):
    """Transport protocol for CDP JSON-RPC calls.

    The real implementation is :class:`_WebSocketCdpClient`; tests inject a
    ``FakeCdpClient`` (provided by the test suite, not this module).
    """

    def call(self, method: str, params: Optional[dict] = None, *,
             timeout: Optional[float] = None) -> dict:
        """Send a CDP method and return the result dict.

        Raises :class:`CdpError` on a CDP-level error.
        """
        ...

    def close(self) -> None:
        """Close the transport. Idempotent, never raises."""
        ...


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Bind to an ephemeral port on 127.0.0.1 and return the port number.

    The socket is closed before returning, so there is a tiny race — acceptable for
    the short window before Chrome binds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return cast(int, s.getsockname()[1])


def _wait_for_port(host: str, port: int, timeout: float) -> None:
    """Poll ``http://{host}:{port}/json/version`` until it returns 200.

    Raises :class:`TimeoutError` if the port is not reachable within *timeout*
    seconds.
    """
    url = f"http://{host}:{port}/json/version"
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # close each poll (audit r3)
                if resp.status == 200:
                    return
        except Exception as exc:
            last_err = exc
        time.sleep(0.2)
    raise TimeoutError(
        f"Timed out waiting for {url} (>{timeout}s): {last_err or 'no response'}"
    )


def _discover_page_ws(host: str, port: int, target_id: Optional[str],
                      timeout: float) -> str:
    """GET ``http://{host}:{port}/json`` and return the ``webSocketDebuggerUrl``
    of a page target.

    If *target_id* is given, the target must have that ``id``.  Otherwise the
    first target with ``type == "page"`` and a non-empty ``webSocketDebuggerUrl``
    is used.

    Raises :class:`CdpError` if no suitable target is found within *timeout*.
    """
    url = f"http://{host}:{port}/json"
    deadline = time.monotonic() + timeout
    last_err: Optional[str] = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # close each poll (audit r3)
                status = resp.status
                body = resp.read() if status == 200 else b""
            if status != 200:
                last_err = f"HTTP {status}"
            else:
                targets = json.loads(body.decode("utf-8"))
                for t in targets:
                    if t.get("type") != "page":
                        continue
                    ws_url = t.get("webSocketDebuggerUrl") or ""
                    if not ws_url:
                        continue
                    if target_id is not None and t.get("id") != target_id:
                        continue
                    return ws_url
                last_err = "no matching page target found"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(0.2)
    raise CdpError(
        "TargetDiscovery",
        f"Could not discover page websocket URL (host={host}, port={port}, "
        f"target_id={target_id!r}) within {timeout}s: {last_err}"
    )


def _resolve_chrome(chrome_path: Optional[str] = None) -> str:
    """Resolve the Chrome executable path.

    Resolution order:
    1. *chrome_path* argument (if not None)
    2. ``$BOBCLAW_CHROME`` environment variable
    3. Standard Windows install paths
    4. ``shutil.which("chrome")`` or ``shutil.which("chrome.exe")``

    Raises :class:`CdpError` if no Chrome is found.
    """
    if chrome_path is not None:
        if os.path.isfile(chrome_path):
            return chrome_path
        raise CdpError("ChromeResolution",
                        f"Specified chrome_path does not exist: {chrome_path!r}")

    env_path = os.environ.get("BOBCLAW_CHROME")
    if env_path:
        if os.path.isfile(env_path):
            return env_path
        raise CdpError("ChromeResolution",
                        f"$BOBCLAW_CHROME set but not a file: {env_path!r}")

    # Standard Windows install paths
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    # shutil.which
    resolved = shutil.which("chrome") or shutil.which("chrome.exe")
    if resolved:
        return resolved

    raise CdpError("ChromeResolution",
                    "No Chrome executable found — supply chrome_path, set "
                    "$BOBCLAW_CHROME, or install Chrome.")


def _box_center(bounds: Tuple[int, int, int, int]) -> Tuple[int, int]:
    """Return ``(cx, cy)`` — the centre of ``(x, y, w, h)``."""
    x, y, w, h = bounds
    return (x + w // 2, y + h // 2)


_KEY_TABLE: dict[str, dict[str, object]] = {
    "Enter":    {"key": "Enter",    "code": "Enter",    "windowsVirtualKeyCode": 13},
    "Tab":      {"key": "Tab",      "code": "Tab",      "windowsVirtualKeyCode": 9},
    "Escape":   {"key": "Escape",   "code": "Escape",   "windowsVirtualKeyCode": 27},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "Delete":   {"key": "Delete",   "code": "Delete",   "windowsVirtualKeyCode": 46},
    "ArrowUp":  {"key": "ArrowUp",  "code": "ArrowUp",  "windowsVirtualKeyCode": 38},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "Home":     {"key": "Home",     "code": "Home",     "windowsVirtualKeyCode": 36},
    "End":      {"key": "End",      "code": "End",      "windowsVirtualKeyCode": 35},
    # Space is the one PRINTABLE key in the table — it needs "text" so keyDown actually
    # inserts a space character (audit r2); the non-printable keys above need no text.
    "Space":    {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "},
}


def _terminate_process(proc: subprocess.Popen) -> None:
    """Kill *proc* by its OWN pid — best-effort, idempotent, used by both ``close()`` and the
    ``launch()`` error path (audit r2: the error path must be as forceful as close()).

    On Windows a belt-and-suspenders PowerShell ``Stop-Process -Id <pid>`` follows
    ``terminate()`` (by PID ONLY — never a broad ``chrome.exe`` kill; the operator has his own
    Chrome open).
    """
    try:
        proc.terminate()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Stop-Process -Id {proc.pid} -Force -ErrorAction SilentlyContinue"],
                capture_output=True,
                timeout=10.0,
            )
        except Exception:
            pass
    else:
        try:
            proc.kill()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Real websocket transport (lazy-imported)
# ---------------------------------------------------------------------------

class _WebSocketCdpClient:
    """CDP transport over a single ``websockets.sync`` connection.

    Auto-incrementing message id; reads frames until the matching id is received.
    Events (frames without an ``id``) are discarded.  Out-of-order responses are
    buffered by id.
    """

    _RECV_TIMEOUT = 30.0  # default per-frame read timeout (s) — never block forever

    def __init__(self, ws: object) -> None:
        # ws is a websockets.sync.client.ClientConnection — type is hidden behind
        # the lazy import.
        self._ws = ws
        self._id = 0
        self._pending: dict[int, dict] = {}

    def call(self, method: str, params: Optional[dict] = None, *,
             timeout: Optional[float] = None) -> dict:
        self._id += 1
        msg_id = self._id
        payload = json.dumps({"id": msg_id, "method": method,
                              "params": params or {}})
        self._ws.send(payload)

        recv_timeout = timeout if timeout is not None else self._RECV_TIMEOUT
        while True:
            # Check if this response was already buffered (out-of-order safety).
            if msg_id in self._pending:
                frame = self._pending.pop(msg_id)
                if "error" in frame:
                    raise CdpError(method, frame["error"])
                return frame.get("result", {})

            raw = self._ws.recv(timeout=recv_timeout)
            frame = json.loads(raw)

            if "id" not in frame:
                # CDP event — discard
                continue

            if frame["id"] == msg_id:
                if "error" in frame:
                    raise CdpError(method, frame["error"])
                return frame.get("result", {})

            # A frame whose id != msg_id. In strictly-serial CDP there is at most one
            # in-flight command, so any non-matching id is a LATE/stale response from a
            # prior (e.g. timed-out) call whose caller has moved on — DISCARD it. No
            # response can arrive for an id we have not sent yet, so there is nothing to
            # buffer: _pending is bounded BY CONSTRUCTION (it stays empty). This removes
            # the provably-dead future-buffer branch (audit r2/r3). The top-of-loop
            # `msg_id in self._pending` fast-path is kept as harmless defensive code.
            continue

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CdpSurface — the public adapter
# ---------------------------------------------------------------------------

class CdpSurface(Surface):
    """CDP/browser Surface adapter.

    Drives Chrome over the Chrome DevTools Protocol via a JSON-RPC websocket.
    Every public method is safe (``act`` never raises; ``close`` is idempotent).
    """

    def __init__(self, client: CdpClient, *,
                 viewport: Optional[Tuple[int, int]] = None,
                 default_scroll_amount: int = 100,
                 _process: Optional[subprocess.Popen] = None,
                 _user_data_dir: Optional[str] = None,
                 _owns_process: bool = False) -> None:
        self._client = client
        self._viewport = viewport          # fallback size if getLayoutMetrics fails
        self._default_scroll_amount = default_scroll_amount
        self._process = _process
        self._user_data_dir = _user_data_dir
        self._owns_process = _owns_process
        self._seq = 0
        # Cache the last-known viewport size from capture (for scroll centre)
        self._last_size: Tuple[int, int] = viewport or (0, 0)

    # -- Class factories ---------------------------------------------------

    @classmethod
    def connect(cls, port: int, *, host: str = "127.0.0.1",
                target_id: Optional[str] = None,
                viewport: Optional[Tuple[int, int]] = None,
                connect_timeout: float = 10.0) -> CdpSurface:
        """Discover a page target and connect to it over CDP.

        Args:
            port: Remote debugging port.
            host: Chrome host.
            target_id: Optional target id to match.
            viewport: Fallback viewport size.
            connect_timeout: Max seconds to wait for target discovery + the open.

        Returns:
            A connected :class:`CdpSurface` (does NOT own the Chrome process).
        """
        ws_url = _discover_page_ws(host, port, target_id,
                                   timeout=connect_timeout)

        # Lazy import of the real synchronous websocket client — kept out of module
        # top so importing this module pulls in no websocket dependency.
        import websockets.sync.client as ws_client

        # open_timeout (NOT timeout) is the connect kwarg; max_size=None so a large
        # base64 Page.captureScreenshot frame is never truncated/rejected.
        raw_ws = ws_client.connect(ws_url, open_timeout=connect_timeout, max_size=None)
        client = _WebSocketCdpClient(raw_ws)

        surface = cls(client, viewport=viewport)

        # Enable required CDP domains. If any enable fails, CLOSE the client we just
        # opened (audit r1: otherwise the websocket is orphaned on the error path).
        enables = ["Page.enable", "DOM.enable",
                   "Accessibility.enable", "Runtime.enable"]
        try:
            for method in enables:
                client.call(method)
        except Exception:
            client.close()
            raise

        return surface

    @classmethod
    def launch(cls, *, chrome_path: Optional[str] = None,
               url: str = "about:blank",
               user_data_dir: Optional[str] = None,
               port: Optional[int] = None,
               headless: bool = True,
               window_size: Tuple[int, int] = (1280, 1024),
               extra_args: Iterable[str] = (),
               launch_timeout: float = 30.0,
               viewport: Optional[Tuple[int, int]] = None) -> CdpSurface:
        """Launch a fresh Chrome instance and connect to it.

        Args:
            chrome_path: Path to the Chrome executable (resolved via
                :func:`_resolve_chrome` if ``None``).
            url: Initial URL to load.
            user_data_dir: Chrome user data directory (a throwaway temp dir
                if ``None``).
            port: Remote debugging port (free ephemeral port if ``None``).
            headless: If ``True``, add ``--headless=new``.
            window_size: ``--window-size=W,H`` argument.
            extra_args: Additional command-line arguments.
            launch_timeout: Max seconds to wait for Chrome to open the
                debugging port.
            viewport: Fallback viewport size (defaults to *window_size*).

        Returns:
            A connected :class:`CdpSurface` that owns the Chrome process and
            (if it created one) the throwaway user data directory.
        """
        # Resolve Chrome
        chrome_exe = _resolve_chrome(chrome_path)

        # Allocate resources
        used_port = port if port is not None else _free_port()
        if user_data_dir is None:
            user_data_dir = tempfile.mkdtemp(prefix="bobclaw_cdp_")
            owns_udd = True
        else:
            owns_udd = False

        # Build command
        cmd = [chrome_exe]
        if headless:
            cmd.append("--headless=new")
        cmd.extend([
            f"--remote-debugging-port={used_port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--window-size={window_size[0]},{window_size[1]}",
        ])
        cmd.extend(extra_args)
        cmd.append(url)

        # Spawn Chrome, wait for the debug port, and connect — all under ONE guard so a
        # failure at ANY step (incl. Popen itself, e.g. a bad chrome path) never leaks the
        # spawned process or the throwaway user-data-dir (audit r1).
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _wait_for_port("127.0.0.1", used_port, timeout=launch_timeout)
            # Connect (does NOT own the process — we do).
            surface = cls.connect(
                used_port,
                host="127.0.0.1",
                target_id=None,
                viewport=viewport or window_size,
                connect_timeout=launch_timeout,
            )
        except Exception:
            if proc is not None:
                _terminate_process(proc)
            if owns_udd:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            raise

        surface._process = proc
        surface._user_data_dir = user_data_dir if owns_udd else None
        surface._owns_process = True
        return surface

    # -- Surface ABC -------------------------------------------------------

    def capture(self) -> Frame:
        """Capture a fresh frame (screenshot hash + a11y tree + viewport size).

        The screenshot bytes are hashed and immediately discarded — they are
        never stored on the :class:`Frame` or on ``self``.
        """
        self._seq += 1

        # 1. Pixel hash
        shot = self._client.call("Page.captureScreenshot", {})
        raw = base64.b64decode(shot["data"])
        image_hash = hash_bytes(raw)
        # raw goes out of scope here — never stored

        # 2. Viewport size
        try:
            lm = self._client.call("Page.getLayoutMetrics", {})
            css = lm.get("cssLayoutViewport", {})
            w = int(css.get("clientWidth", 0))
            h = int(css.get("clientHeight", 0))
        except Exception:
            w, h = self._viewport or (0, 0)
        if w <= 0 or h <= 0:
            w, h = self._viewport or (1280, 1024)
        self._last_size = (w, h)

        # 3. Accessibility tree
        ax = self._client.call("Accessibility.getFullAXTree", {})
        nodes: list[A11yNode] = []
        for n in ax.get("nodes", []):
            if n.get("ignored"):
                continue
            role_obj = n.get("role") or {}
            role = role_obj.get("value", "")
            if not role:
                continue
            name = (n.get("name") or {}).get("value", "")
            value = (n.get("value") or {}).get("value", "")
            bid = n.get("backendDOMNodeId")
            node_id = str(bid) if bid is not None else str(n.get("nodeId", ""))
            bounds: Optional[Tuple[int, int, int, int]] = None
            if bid is not None:
                try:
                    content = self._client.call(
                        "DOM.getBoxModel", {"backendNodeId": bid}
                    )["model"]["content"]
                    xs = content[0::2]
                    ys = content[1::2]
                    bounds = (int(min(xs)), int(min(ys)),
                              int(max(xs) - min(xs)), int(max(ys) - min(ys)))
                except Exception:
                    bounds = None
            nodes.append(
                A11yNode(role=role, name=name, value=value,
                         node_id=node_id, bounds=bounds)
            )

        return Frame(seq=self._seq, size=(w, h),
                     image_hash=image_hash, a11y=tuple(nodes))

    def act(self, action: Action) -> ActionResult:
        """Actuate *action* over CDP.  Never raises — returns
        :class:`ActionResult` with ``performed=False`` on any failure.

        Silent-failure (performed=True with no state change) is caught
        downstream by frame-diff, never by this method.
        """
        try:
            return self._act_impl(action)
        except Exception as exc:
            return ActionResult(
                performed=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _act_impl(self, action: Action) -> ActionResult:
        kind = action.kind

        # -- NOOP ------------------------------------------------
        if kind == ActionKind.NOOP:
            return ActionResult(performed=True)

        # -- CLICK / TYPE (focus) coord resolution ----------------
        def _resolve_click_xy() -> Optional[Tuple[int, int]]:
            if action.coord is not None:
                return action.coord
            if action.target:
                # Capture freshest frame to resolve target
                frame = self.capture()
                idx: dict[str, A11yNode] = {}
                for node in frame.a11y:
                    key = node.node_id if node.node_id else f"{node.role}:{node.name}"
                    idx[key] = node
                # Try node_id match, then role:name (via idx), then name match
                candidate: Optional[A11yNode] = None
                if action.target in idx:
                    candidate = idx[action.target]
                else:
                    for node in frame.a11y:
                        if node.name == action.target:
                            candidate = node
                            break
                if candidate is not None and candidate.bounds is not None:
                    return _box_center(candidate.bounds)
            return None

        # -- CLICK ------------------------------------------------
        if kind == ActionKind.CLICK:
            xy = _resolve_click_xy()
            if xy is None:
                return ActionResult(
                    performed=False,
                    error="CLICK requires coord or a resolvable target",
                )
            x, y = xy
            self._client.call("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": x, "y": y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            })
            self._client.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": x, "y": y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            })
            return ActionResult(performed=True)

        # -- TYPE -------------------------------------------------
        if kind == ActionKind.TYPE:
            if action.coord is not None or action.target:
                # Click to focus first
                click_act = Action(kind=ActionKind.CLICK,
                                   target=action.target,
                                   coord=action.coord)
                focus_result = self._act_impl(click_act)
                if not focus_result.performed:
                    return focus_result
            self._client.call("Input.insertText", {"text": action.text})
            return ActionResult(performed=True)

        # -- KEY --------------------------------------------------
        if kind == ActionKind.KEY:
            key_str = action.key
            entry = _KEY_TABLE.get(key_str)
            if entry is not None:
                key_params = dict(entry)
                key_params["type"] = "keyDown"
                self._client.call("Input.dispatchKeyEvent", key_params)
                key_params = dict(entry)
                key_params["type"] = "keyUp"
                self._client.call("Input.dispatchKeyEvent", key_params)
            elif len(key_str) == 1:
                # Single character
                self._client.call("Input.dispatchKeyEvent", {
                    "type": "keyDown",
                    "key": key_str,
                    "text": key_str,
                })
                self._client.call("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                    "key": key_str,
                    "text": key_str,
                })
            else:
                return ActionResult(
                    performed=False,
                    error=f"Unknown key: {key_str!r}",
                )
            return ActionResult(performed=True)

        # -- SCROLL -----------------------------------------------
        if kind == ActionKind.SCROLL:
            if action.coord is not None:
                x, y = action.coord
            else:
                # Viewport centre
                x = self._last_size[0] // 2
                y = self._last_size[1] // 2
            amount = action.amount if action.amount != 0 else self._default_scroll_amount
            direction = action.direction.lower()
            if direction == "down":
                dx, dy = 0, amount
            elif direction == "up":
                dx, dy = 0, -amount
            elif direction == "right":
                dx, dy = amount, 0
            elif direction == "left":
                dx, dy = -amount, 0
            else:
                return ActionResult(
                    performed=False,
                    error=f"Unknown scroll direction: {action.direction!r}",
                )
            self._client.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": x, "y": y,
                "deltaX": dx,
                "deltaY": dy,
            })
            return ActionResult(performed=True)

        # Fallback — should never reach here (ActionKind is exhaustive)
        return ActionResult(performed=False,
                            error=f"Unhandled action kind: {kind}")

    def navigate(self, url: str, *, settle_timeout: float = 10.0) -> None:
        """Navigate to *url* and wait for ``document.readyState == 'complete'``.

        Args:
            url: The URL to navigate to.
            settle_timeout: Max seconds to wait for page load.

        Raises:
            CdpError: on a CDP error.
            TimeoutError: if the page does not reach 'complete'.
        """
        self._client.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + settle_timeout
        while time.monotonic() < deadline:
            result = self._client.call(
                "Runtime.evaluate",
                {"expression": "document.readyState",
                 "returnByValue": True},
            )
            state = (result.get("result") or {}).get("value", "")
            if state == "complete":
                return
            time.sleep(0.1)
        raise TimeoutError(
            f"Page did not reach 'complete' within {settle_timeout}s"
        )

    def close(self) -> None:
        """Idempotent teardown: close the CDP client; if we own the Chrome
        process, kill it BY PID (PowerShell ``Stop-Process`` on Windows — never a
        broad chrome kill) and remove the throwaway user data directory.
        """
        # 1. Close the transport
        try:
            self._client.close()
        except Exception:
            pass

        # 2. Kill the owned process (by PID only, never broad kill) — shared with the
        #    launch() error path so both teardown routes are equally forceful.
        if self._owns_process and self._process is not None:
            _terminate_process(self._process)
            self._process = None

        # 3. Remove owned user data directory
        if self._user_data_dir is not None:
            shutil.rmtree(self._user_data_dir, ignore_errors=True)
            self._user_data_dir = None

    def __enter__(self) -> CdpSurface:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
