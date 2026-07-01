import sys
from pathlib import Path
from core.gui.types import Action, ActionKind, Frame
from core.gui.grounders.holo import (HOLO_BACKEND, parse_coord, GroundOutcome, HoloClient,
                                     HoloGrounder)
from core.verify.postcondition import (FAMILY_BY_BACKEND, family_of, is_decorrelated,
                                       decorrelated_critic_backend)


def resp(content, pt=10, ct=5):
    return {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


FR = Frame(seq=1, size=(1280, 1024), image_hash="h", a11y=())


def test_parse_coord_formats():
    # standard [x, y]
    assert parse_coord("[100, 224]") == (100, 224)
    # JSON object
    assert parse_coord('{"x":100,"y":228}') == (100, 228)
    # parentheses
    assert parse_coord("(103, 310)") == (103, 310)
    # bare numbers
    assert parse_coord("100, 224") == (100, 224)
    # bbox center ([x1,y1,x2,y2])
    assert parse_coord("[10,20,30,40]") == (20, 30)
    # think stripped
    assert parse_coord("<think>junk 9,9</think>[5, 6]") == (5, 6)
    # no numbers -> None
    assert parse_coord("no numbers here") is None
    # single number -> None
    assert parse_coord("42") is None
    # floats round
    assert parse_coord("[1.4, 2.6]") == (1, 3)


def test_holo_client_ground_point_parses_coord():
    captured = {}

    def post(body):
        captured["body"] = body
        return resp("[120, 240]")

    c = HoloClient(_post=post)
    out = c.ground_point(b"PNGBYTES", "the Save button")
    assert out.coord == (120, 240)
    assert out.completion_tokens == 5
    assert out.prompt_tokens == 10

    # Check request body structure
    body = captured["body"]
    # temperature and max_tokens
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 64
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # messages: one user message with text part and image_url part
    messages = body["messages"]
    assert len(messages) == 1
    msg = messages[0]
    assert msg["role"] == "user"
    content = msg["content"]
    assert isinstance(content, list)
    # Find text part containing "Localize" and instruction
    text_parts = [c for c in content if c["type"] == "text"]
    assert len(text_parts) == 1
    assert "Localize" in text_parts[0]["text"]
    assert "the Save button" in text_parts[0]["text"]
    # Find image_url part
    img_parts = [c for c in content if c["type"] == "image_url"]
    assert len(img_parts) == 1
    assert img_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_holo_client_ground_point_no_coord():
    # _post returns response with no coordinate -> coord is None
    c = HoloClient(_post=lambda body: resp("I cannot find it"))
    out = c.ground_point(b"dummy", "find it")
    assert out.coord is None
    # _post raises exception -> coord is None, raw is ""
    def raise_post(body):
        raise RuntimeError("network error")
    c = HoloClient(_post=raise_post)
    out = c.ground_point(b"dummy", "find it")
    assert out.coord is None
    assert out.raw == ""


def test_holo_client_health():
    # _health returns True
    c = HoloClient(_health=lambda: True)
    assert c.health_check() is True
    # _health returns False
    c = HoloClient(_health=lambda: False)
    assert c.health_check() is False
    # _health raises -> health_check returns False (never raises)
    def raise_health():
        raise RuntimeError("down")
    c = HoloClient(_health=raise_health)
    assert c.health_check() is False


def test_holo_grounder_click_action():
    post = lambda body: resp("[50, 60]")
    c = HoloClient(_post=post)
    g = HoloGrounder(c, screenshot_provider=lambda: b"PNG")
    act = g.ground("the button", FR)
    assert isinstance(act, Action)
    assert act.kind == ActionKind.CLICK
    assert act.coord == (50, 60)


def test_holo_grounder_none_paths():
    # provider returns b"" -> None
    g = HoloGrounder(HoloClient(_post=lambda body: resp("some")), screenshot_provider=lambda: b"")
    assert g.ground("x", FR) is None
    # provider returns None -> None
    g = HoloGrounder(HoloClient(_post=lambda body: resp("some")), screenshot_provider=lambda: None)
    assert g.ground("x", FR) is None
    # provider raises -> None
    def raise_provider():
        raise OSError("no screenshot")
    g = HoloGrounder(HoloClient(_post=lambda body: resp("some")), screenshot_provider=raise_provider)
    assert g.ground("x", FR) is None
    # head returns no coord (post returns no coord) -> None
    g = HoloGrounder(HoloClient(_post=lambda body: resp("nope")), screenshot_provider=lambda: b"PNG")
    assert g.ground("x", FR) is None


def test_decorrelation_registered():
    # HOLO_BACKEND should be "holo_grounder"
    assert HOLO_BACKEND == "holo_grounder"
    # family_of returns "holo"
    assert family_of(HOLO_BACKEND) == "holo"
    # HOLO_BACKEND must be in FAMILY_BY_BACKEND (registration)
    assert HOLO_BACKEND in FAMILY_BY_BACKEND
    # Decorrelation with all listed backends
    for b in ("deepseek_v4_flash", "glm_5_2", "claude_api", "claude_code",
              "gemini_pro", "kimi_code", "minimax", "local"):
        assert is_decorrelated(HOLO_BACKEND, b) is True
    # Same-backend decorrelation is False
    assert is_decorrelated(HOLO_BACKEND, HOLO_BACKEND) is False
    # decorrelated_critic_backend returns a non‑holo backend
    crit = decorrelated_critic_backend(HOLO_BACKEND)
    assert family_of(crit) != "holo"
    assert crit in FAMILY_BY_BACKEND


def test_holo_client_malformed_response_never_raises():
    # audit r1 focus-6: a returned-but-malformed response (non-dict body, JSON list, a non-dict
    # choice/message, a non-dict usage) must yield coord None — never raise out of ground_point.
    for bad in (
        lambda body: ["not", "a", "dict"],            # JSON list, not a dict
        lambda body: "a bare string",                  # non-dict scalar
        lambda body: {"choices": "oops"},              # choices not a list
        lambda body: {"choices": ["not-a-dict"]},      # choice not a dict
        lambda body: {"choices": [{"message": "x"}]},  # message not a dict
        lambda body: {"choices": [{"message": {}}], "usage": "nope"},  # usage not a dict
        lambda body: {},                               # empty dict
    ):
        out = HoloClient(_post=bad).ground_point(b"PNG", "the button")
        assert out.coord is None  # no crash, surfaced as a MISS
    # message.content as a LIST of parts (legal OpenAI multimodal shape) must not crash parse_coord
    list_resp = {"choices": [{"message": {"content": [{"type": "text", "text": "[1,2]"}]}}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    assert HoloClient(_post=lambda body: list_resp).ground_point(b"PNG", "x").coord is None
    # a non-bytes png to the PUBLIC ground_point must not raise (base64 of None) -> MISS
    assert HoloClient(_post=lambda body: resp("[1,2]")).ground_point(None, "x").coord is None


def test_parse_coord_non_str_is_none():
    # type-safe: parse_coord never raises on non-str input (audit r2 focus-0)
    assert parse_coord([1, 2]) is None
    assert parse_coord(None) is None
    assert parse_coord({"x": 1, "y": 2}) is None  # a dict is not parsed positionally (no crash)


def test_import_purity():
    src = Path(__file__).resolve().parents[2] / "core/gui/grounders/holo.py"
    text = src.read_text()
    for bad in ("core.backends", "core.nodes", "aiohttp", "import requests", "httpx"):
        assert bad not in text
