"""MS9 U5 — the gateway forwards the Ask-Bob helper bubble's additive ``page_context``.

Contract (the chat WS → core forward truth table documented on ``_page_context_field``): a
page_context-free ``message`` frame — every existing client and the main chat screen — forwards
a BYTE-IDENTICAL upstream payload (no key added). Only the helper bubble, which sends a
non-empty ``page_context`` dict, adds ``{"page_context": {...}}``. The field carries screen
context only, never capability, so — unlike the P3 scope — it needs no HMAC vouch. Core
additionally flag-gates the splice, so a forwarded page_context still changes nothing until
``PAGE_CONTEXT_ENABLED`` is on.
"""
from routers.chat import _page_context_field


def test_absent_page_context_forwards_nothing():
    # No page_context key (ordinary chat turn) ⇒ {} ⇒ byte-identical upstream payload.
    assert _page_context_field({"content": "hi", "conversation_id": "c1"}) == {}
    assert _page_context_field({}) == {}
    assert _page_context_field(None) == {}


def test_null_or_empty_or_wrong_type_forwards_nothing():
    # Defensive: only a NON-EMPTY dict is forwarded — null / {} / a string all add nothing.
    assert _page_context_field({"page_context": None}) == {}
    assert _page_context_field({"page_context": {}}) == {}
    assert _page_context_field({"page_context": "teams"}) == {}
    assert _page_context_field({"page_context": ["teams"]}) == {}


def test_non_empty_page_context_is_forwarded_verbatim():
    pc = {"page": "teams", "snapshot": {"teams": ["core"], "selected": "core"}}
    out = _page_context_field({"content": "what's here?", "page_context": pc})
    assert out == {"page_context": pc}
    # Forwarded by reference-equal value — no reshaping in the gateway.
    assert out["page_context"] is pc
