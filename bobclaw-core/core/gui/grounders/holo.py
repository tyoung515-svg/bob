from __future__ import annotations

"""
MS2-G4 Holo-3.1 grounding-head HTTP adapter.

Implements the ``Grounder`` Protocol for the §4 GUI lane using a head‑agnostic
sync stdlib ``urllib`` client.  The locked serving + request facts from
RESULTS‑S0 are hard‑coded: ``temperature=0``, ``max_tokens=64``,
``chat_template_kwargs={enable_thinking: <self.enable_thinking>}``, the image
as a ``data:image/png;base64,<b64>`` URL, and the locked localize instruction.

The ``HoloClient`` is head‑agnostic — the model / quant live in the constructor —
and the ``HoloGrounder`` receives the raw PNG via an injected
``screenshot_provider`` closure because the ``Frame`` dataclass holds only
an ``image_hash``, never pixel bytes (DESIGN‑MS‑D1 §3‑G4, §4 contract).

DESIGN‑MS‑D1 §3‑G4:  Holo‑3.1 grounding head with structured‑first fusion
   (a11y primary, pixel fallback, >10 % bbox overlap dedup).
RESULTS‑S0:           Locked server command, request shape, and accuracy
   baseline (hit@16 px 100 %, mean 2.5 px, decode ~55–60 tok/s).
"""

import base64
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from core.gui.types import Action, ActionKind, Frame

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

HOLO_BACKEND: str = "holo_grounder"

# ---------------------------------------------------------------------------
# Coordinate parser (verbatim from S0 harness)
# ---------------------------------------------------------------------------


def parse_coord(text: str) -> tuple[int, int] | None:
    """Parse a Holo output into pixel coordinates (x, y).

    Handles:
    - JSON array ``[100, 224]``
    - JSON object ``{"x":100,"y":228}``
    - Parenthesised tuple ``(103, 310)``
    - Bare ``100, 224``
    - 4-number bbox ``[x1,y1,x2,y2]`` (returns centre)
    - Strip leading ``<think>...</think>`` and any surrounding prose
    - Round floats to int
    Returns ``None`` if no coordinate can be extracted.
    """
    # Type-safe: a non-str input (e.g. a list-shaped message.content) is a MISS, never a crash
    # (the Grounder never-raises contract; audit r2 focus-0).
    if not isinstance(text, str):
        return None
    # Strip <think> blocks and leading/trailing prose
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Keep only numbers and separators (commas, spaces, brackets)
    # Extract all signed integers/floats
    numbers = [float(m) for m in re.findall(r"-?\d+\.?\d*", text)]
    if not numbers:
        return None
    # Round to int
    ints = [round(n) for n in numbers]
    if len(ints) >= 4:
        # Bounding box [x1,y1,x2,y2] -> centre
        x1, y1, x2, y2 = ints[0], ints[1], ints[2], ints[3]
        return ((x1 + x2) // 2, (y1 + y2) // 2)
    if len(ints) >= 2:
        return (ints[0], ints[1])
    return None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundOutcome:
    """Result of a single grounding query."""

    coord: tuple[int, int] | None
    raw: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0


class HoloError(RuntimeError):
    """Raised by the optional strict path; the Grounder NEVER raises."""
    pass


# ---------------------------------------------------------------------------
# Holo client (stdlib urllib, injectable transport)
# ---------------------------------------------------------------------------


class HoloClient:
    """Thin sync client for the Holo‑3.1 grounding server.

    Uses the LOCKED request format (RESULTS‑S0): ``temperature=0``,
    ``max_tokens=64``, ``chat_template_kwargs={enable_thinking: <bool>}``,
    image as ``data:image/png;base64,<b64>``.

    The ``_post`` and ``_health`` seams allow injection for unit tests
    (no real server needed).

    Parameters
    ----------
    base_url:
        Root URL of the llama.cpp server (e.g. ``http://127.0.0.1:8090``).
    model:
        Model identifier sent in the request body.
    enable_thinking:
        Whether to enable the reasoning/thinking mode.
    timeout:
        HTTP request timeout in seconds.
    _post:
        Injectable transport: takes the request body dict, returns the
        raw OpenAI‑style response dict.  ``None`` → real ``urllib`` POST.
    _health:
        Injectable health check.  ``None`` → real ``urllib`` GET ``/health``.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8090",
        model: str = "q4_k_m.gguf",
        *,
        enable_thinking: bool = False,
        timeout: float = 300.0,
        _post: Callable[[dict], dict] | None = None,
        _health: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enable_thinking = enable_thinking
        self.timeout = timeout
        self._post = _post
        self._health = _health

    # ---- internal helpers ---------------------------------------------------

    def _build_body(
        self, text: str, png_bytes: bytes, max_tokens: int
    ) -> dict:
        """Build the LOCKED grounding request body.

        Returns a dict suitable for JSON serialisation / ``_post``.
        """
        b64_png = base64.b64encode(png_bytes).decode("ascii")
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_png}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {
                "enable_thinking": self.enable_thinking
            },
        }

    def _chat(
        self, text: str, png_bytes: bytes, max_tokens: int
    ) -> tuple[str, dict, float]:
        """Send an OpenAI‑style chat completion request.

        Returns ``(content, usage_dict, wall_clock_seconds)``.
        ``usage_dict`` has keys ``prompt_tokens`` and ``completion_tokens``
        (both 0 if missing).  On any error returns ``("", {}, 0.0)``.
        """
        # _build_body is inside the guard: base64.b64encode(png_bytes) raises on a non-bytes/None png,
        # and ground_point is public + contracted never-raises (audit r2 focus-0).
        wall = 0.0
        try:
            body = self._build_body(text, png_bytes, max_tokens)
        except Exception:
            return ("", {"prompt_tokens": 0, "completion_tokens": 0}, 0.0)

        if self._post is not None:
            try:
                t0 = time.monotonic()
                resp = self._post(body)
                wall = time.monotonic() - t0
            except Exception:
                return ("", {"prompt_tokens": 0, "completion_tokens": 0}, 0.0)
        else:
            url = f"{self.base_url}/v1/chat/completions"
            headers = {"Content-Type": "application/json"}
            data = json.dumps(body).encode("utf-8")
            try:
                t0 = time.monotonic()
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    resp_body = json.loads(response.read().decode("utf-8"))
                wall = time.monotonic() - t0
                resp = resp_body
            except Exception:
                return ("", {"prompt_tokens": 0, "completion_tokens": 0}, 0.0)

        # Extract content + usage DEFENSIVELY — a malformed-but-returned response (a non-dict body, a
        # JSON list, missing/odd-typed choices/message/usage) must yield the safe empty result, NEVER
        # raise: a parse/shape error is a grounding MISS, not a crash (the Grounder contract). The
        # extraction was previously outside the request guard, so a non-dict response could escape
        # ground_point (audit r1 focus-6).
        try:
            if not isinstance(resp, dict):
                return ("", {"prompt_tokens": 0, "completion_tokens": 0}, wall)
            choices = resp.get("choices") or []
            content = ""
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                if isinstance(msg, dict):
                    content = msg.get("content", "") or ""
            # content may legally be a LIST of parts (OpenAI multimodal shape); coerce anything
            # non-str to "" so the (str-only) parse_coord can never raise (audit r2 focus-0).
            if not isinstance(content, str):
                content = ""
            usage = resp.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            prompt_tokens = usage.get("prompt_tokens", 0) or 0
            completion_tokens = usage.get("completion_tokens", 0) or 0
        except Exception:
            return ("", {"prompt_tokens": 0, "completion_tokens": 0}, wall)

        return (content, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}, wall)

    # ---- public API ---------------------------------------------------------

    def health_check(self) -> bool:
        """Check server health via ``GET /health``.

        Returns ``True`` if the server responded with HTTP 200.
        Never raises — returns ``False`` on any error.
        """
        if self._health is not None:
            try:
                return self._health()
            except Exception:
                return False

        url = f"{self.base_url}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.status == 200
        except Exception:
            return False

    def ground_point(self, png_bytes: bytes, subgoal: str) -> GroundOutcome:
        """Ground a subgoal on a screenshot.

        Builds the LOCKED instruction + request, sends it, parses the
        response, and returns a ``GroundOutcome``.  NEVER raises — on
        any error returns ``GroundOutcome(coord=None, raw="", ...)``.

        Parameters
        ----------
        png_bytes:
            Raw PNG image bytes.
        subgoal:
            Natural‑language subgoal (e.g. "the Save changes button").

        Returns
        -------
        GroundOutcome
            Contains the parsed pixel coordinate (or ``None`` if parsing
            failed or the server returned no usable coordinate), the raw
            response string, token usage, and wall‑clock latency.
        """
        text = (
            "Localize the element matching the instruction and output a single "
            "click point as pixel coordinates [x, y].\nInstruction: " + subgoal
        )
        content, usage, wall = self._chat(text, png_bytes, max_tokens=64)
        coord = parse_coord(content)
        return GroundOutcome(
            coord=coord,
            raw=content,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_s=wall,
        )


# ---------------------------------------------------------------------------
# Grounder implementation (implements loop.Grounder Protocol)
# ---------------------------------------------------------------------------


class HoloGrounder:
    """Grounder that delegates to a ``HoloClient``.

    The ``screenshot_provider`` seam supplies the raw PNG bytes because
    the ``Frame`` dataclass holds only an ``image_hash``, never pixel data
    (DESIGN‑MS‑D1 §4 contract).  This head is head‑agnostic — the model,
    quant and server URL live in the injected ``HoloClient``.

    Parameters
    ----------
    client:
        Configured ``HoloClient`` instance.
    screenshot_provider:
        Callable that returns the current screenshot as raw PNG bytes,
        or ``None`` if the screenshot cannot be obtained.
    action_kind:
        The kind of action to produce when a coordinate is resolved.
        Defaults to ``ActionKind.CLICK``.
    """

    def __init__(
        self,
        client: HoloClient,
        screenshot_provider: Callable[[], bytes | None],
        *,
        action_kind: ActionKind = ActionKind.CLICK,
    ) -> None:
        self._client = client
        self._screenshot_provider = screenshot_provider
        self._action_kind = action_kind

    def ground(self, subgoal: str, frame: Frame) -> Action | None:
        """Resolve a subgoal to an action using the Holo head.

        Always catches exceptions from the provider / client and returns
        ``None`` if a coordinate cannot be determined.  NEVER raises.

        Parameters
        ----------
        subgoal:
            Natural‑language instruction.
        frame:
            Current frame (used only for type‑conformance; the pixel data
            comes via the injected provider).

        Returns
        -------
        Action | None
            An action with the parsed coordinate, or ``None`` if grounding
            failed.
        """
        try:
            png = self._screenshot_provider()
        except Exception:
            return None
        if not png:
            return None

        outcome = self._client.ground_point(png, subgoal)
        if outcome.coord is None:
            return None

        return Action(kind=self._action_kind, coord=outcome.coord)

    def health_check(self) -> bool:
        """Delegate to the injected client's health check."""
        return self._client.health_check()
