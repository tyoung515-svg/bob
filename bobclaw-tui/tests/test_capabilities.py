"""BoBClaw TUI — CapabilitiesClient + palette arg-completion tests (Wave 1B Task 3).

Fixture-JSON discipline: the gateway shapes (``GET /capabilities`` → faces + merged
backends; ``GET /profiles`` → ``{items}`` envelopes) are pinned as literal documents,
parsed by the pure ``parse_*`` helpers — no live HTTP. ``fetch`` itself is exercised
over the same fake-session pattern as ``test_conversations_client.py``, including the
silent-degrade paths (connection error / non-200 ⇒ ``available`` stays False).
"""
from __future__ import annotations

import asyncio
import json

from bobclaw_tui.commands import (
    CapabilitiesClient,
    parse_face_options,
    parse_model_options,
    parse_profile_options,
)

CAPS_DOC = {
    "faces": [
        {"id": "assistant", "name": "Assistant", "display_name": "Assistant",
         "blurb": "the default helper"},
        {"id": "sage", "name": "Sage", "display_name": None, "blurb": "deep thinker"},
        {"id": "scout", "name": "Scout", "display_name": None, "blurb": None},
    ],
    "backends": [
        {"backend": "lmstudio", "model": "qwen3-8b", "available": True,
         "max_usd_per_worker": None, "max_fanout_width": None},
        {"backend": "ollama", "model": None, "available": False,
         "max_usd_per_worker": 0.0, "max_fanout_width": 2},
        {"backend": "openai", "model": "gpt-5", "available": True,
         "max_usd_per_worker": 1.5, "max_fanout_width": 8},
    ],
    "actions": [],
    "capabilities": {"roles": [], "face_count": 3, "backend_count": 3,
                     "available_backends": ["lmstudio", "openai"], "action_count": 0},
}

PROFILES_DOC = {
    "items": [
        {"name": "council-max", "builtin": True, "roles": {"manager": 1}},
        {"name": "ops-team", "builtin": False, "roles": {"manager": 1}},
    ]
}


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
    """Replays queued ``_Resp`` objects in order, recording every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # (method, url, kwargs)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        assert self._responses, f"unexpected call: GET {url}"
        return self._responses.pop(0)


class _DownSession:
    """Every GET raises — the unreachable-gateway posture."""

    def __init__(self):
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        raise ConnectionError("connection refused")


def _client(session) -> CapabilitiesClient:
    return CapabilitiesClient("127.0.0.1:7836", "tok", session)


# ── pure parsers (fixture JSON, no I/O) ──
def test_parse_face_options_extracts_ids_and_descriptions():
    got = parse_face_options(CAPS_DOC)
    assert got == [
        ("assistant", "Assistant"),      # display_name preferred
        ("sage", "deep thinker"),        # blurb fallback when display_name is null
        ("scout", ""),                   # both null → empty description, still listed
    ]


def test_parse_face_options_tolerates_garbage():
    assert parse_face_options(None) == []
    assert parse_face_options({"faces": None}) == []
    assert parse_face_options({"faces": ["nope", {"no_id": True}, 7]}) == []


def test_parse_profile_options_marks_builtin_and_custom():
    got = parse_profile_options(PROFILES_DOC)
    assert got == [("council-max", "built-in"), ("ops-team", "custom")]
    assert parse_profile_options({"items": [{"no_name": True}]}) == []
    assert parse_profile_options(None) == []


def test_parse_model_options_pairs_model_and_backend():
    got = parse_model_options(CAPS_DOC)
    # ollama has no live model id → skipped; both tokens ride in one option
    assert got == [("qwen3-8b lmstudio", "lmstudio"), ("gpt-5 openai", "openai")]


def test_parse_model_options_marks_unavailable():
    doc = {"backends": [{"backend": "ollama", "model": "llama3", "available": False}]}
    assert parse_model_options(doc) == [("llama3 ollama", "ollama (unavailable)")]


# ── completion filtering (pure over the cached options) ──
def _fetched_client() -> CapabilitiesClient:
    c = _client(_FakeSession([_Resp(200, CAPS_DOC), _Resp(200, PROFILES_DOC)]))
    _run(c.fetch())
    assert c.available
    return c


def test_completions_face_filters_by_prefix():
    c = _fetched_client()
    rows = c.completions("/face s")
    assert [oid for oid, _ in rows] == ["/face sage", "/face scout"]
    rows = c.completions("/face ")
    assert [oid for oid, _ in rows] == ["/face assistant", "/face sage", "/face scout"]


def test_completions_profile_offers_names():
    c = _fetched_client()
    rows = c.completions("/profile council")
    assert [oid for oid, _ in rows] == ["/profile council-max"]
    assert "built-in" in rows[0][1]


def test_completions_model_option_id_carries_backend():
    c = _fetched_client()
    rows = c.completions("/model q")
    assert [oid for oid, _ in rows] == ["/model qwen3-8b lmstudio"]
    assert "lmstudio" in rows[0][1]


def test_completions_empty_when_unavailable():
    c = _client(_FakeSession([]))  # never fetched → static palette only
    assert not c.available
    assert c.completions("/face ") == []


def test_completions_empty_past_first_argument():
    c = _fetched_client()
    assert c.completions("/model qwen3-8b lm") == []  # second token — no completion
    assert c.completions("/help ") == []              # not an arg-completable command
    assert c.completions("plain text") == []


# ── fetch: once per session, authed, silent degrade ──
def test_fetch_pulls_both_endpoints_once_with_bearer_token():
    s = _FakeSession([_Resp(200, CAPS_DOC), _Resp(200, PROFILES_DOC)])
    c = _client(s)
    _run(c.fetch())
    _run(c.fetch())  # cached — no second round-trip
    assert len(s.calls) == 2
    assert s.calls[0][1] == "http://127.0.0.1:7836/capabilities"
    assert s.calls[1][1] == "http://127.0.0.1:7836/profiles"
    assert all(kw["headers"]["Authorization"] == "Bearer tok" for _, _, kw in s.calls)


def test_fetch_degrades_silently_when_gateway_unreachable():
    s = _DownSession()
    c = _client(s)
    _run(c.fetch())  # no exception escapes
    assert not c.available
    assert c.completions("/face ") == []
    assert s.calls == 1  # one attempt, no hammering


def test_fetch_degrades_silently_on_non_200():
    c = _client(_FakeSession([_Resp(502, {"error": "core down"})]))
    _run(c.fetch())
    assert not c.available
    assert c.completions("/face ") == []
