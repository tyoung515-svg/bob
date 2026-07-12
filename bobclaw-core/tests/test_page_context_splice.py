"""MS9 U5 — Ask-Bob helper bubble page_context splice (SPEC §3 / D3).

The page_context field is spliced by execute_node as an additive front-adjacent system
card (the identity-card pattern), FLAG-GATED by ``config.PAGE_CONTEXT_ENABLED``. The hard
accept criterion (U5 #1, the D5-gist invariant): flag OFF ⇒ prompt assembly is
BYTE-IDENTICAL — the splice adds NOTHING regardless of what the client sent.

``page_context_card`` is the SINGLE source of the injection predicate (mirrors
``locale_directive_message``), so these unit tests fully cover the splice decision.
"""
import json

from core.nodes.execute import (
    PAGE_CONTEXT_HEADER,
    _messages_to_prompt,
    locale_directive_message,
    page_context_card,
)

_PC = {"page": "teams", "snapshot": {"teams": ["core", "research"], "selected": "core"}}


# ── The HARD flag gate: OFF ⇒ inject nothing (byte-identical) ────────────────────
def test_card_is_none_when_flag_off_even_with_page_context():
    # The load-bearing invariant: a real, rich page_context still injects NOTHING when the
    # flag is off. This is what makes the assembled prompt byte-identical (U5 accept #1).
    assert page_context_card(_PC, enabled=False) is None
    assert page_context_card({"page": "memory", "snapshot": "x" * 5000}, enabled=False) is None


def test_card_is_none_for_non_injectable_inputs_even_when_flag_on():
    # Absent / None / wrong type / empty dict ⇒ None (no card), even with the flag ON.
    assert page_context_card(None, enabled=True) is None
    assert page_context_card({}, enabled=True) is None
    assert page_context_card("teams", enabled=True) is None
    assert page_context_card(["teams"], enabled=True) is None
    assert page_context_card(123, enabled=True) is None


# ── Flag ON + real page_context ⇒ a deterministic front-adjacent system card ─────
def test_header_bytes():
    # Guards drift of the exact directive text (mirrors test_locale_directive_bytes).
    assert PAGE_CONTEXT_HEADER.startswith("The user is viewing a screen in the BoBClaw app")
    assert "do not invent state" in PAGE_CONTEXT_HEADER


def test_card_with_page_and_dict_snapshot():
    card = page_context_card(_PC, enabled=True)
    assert card["role"] == "system"
    content = card["content"]
    assert content.startswith(PAGE_CONTEXT_HEADER)
    assert "Screen: teams" in content
    # dict snapshot is rendered with sort_keys=True → stable bytes for the same snapshot.
    assert json.dumps(_PC["snapshot"], sort_keys=True, ensure_ascii=False) in content


def test_card_snapshot_rendering_is_deterministic():
    # Same snapshot, keys supplied in different insertion order ⇒ identical card bytes.
    a = page_context_card({"page": "p", "snapshot": {"b": 2, "a": 1}}, enabled=True)
    b = page_context_card({"page": "p", "snapshot": {"a": 1, "b": 2}}, enabled=True)
    assert a == b


def test_card_with_string_snapshot_used_verbatim():
    card = page_context_card({"page": "home", "snapshot": "5 pending approvals"}, enabled=True)
    assert "Screen: home" in card["content"]
    assert "Visible state:\n5 pending approvals" in card["content"]


def test_card_with_page_only_no_snapshot():
    card = page_context_card({"page": "approvals"}, enabled=True)
    assert card["content"] == f"{PAGE_CONTEXT_HEADER}\nScreen: approvals"


# ── The D5-gist invariant: flag-off assembly == no-page_context assembly ─────────
def _assemble(page_context, *, flag: bool) -> list[dict]:
    """Reproduce execute_node's front-adjacent splice sequence (project → page_context →
    locale), the region the U5 change touches, to prove the flag-off prompt is byte-identical."""
    messages = [
        {"role": "system", "content": "face"},
        {"role": "user", "content": "what teams do I have?"},
    ]
    # project context splice (unchanged, always front-adjacent)
    messages.insert(0, {"role": "system", "content": "Project context:\nacme"})
    # U5 page_context splice (the new code) — gated exactly as execute_node gates it
    card = page_context_card(page_context, enabled=flag)
    if card is not None:
        messages.insert(0, card)
    # locale splice (unchanged, front-most)
    d = locale_directive_message("en")  # en ⇒ None ⇒ no insert
    if d is not None:
        messages.insert(0, d)
    return messages


def test_flag_off_prompt_is_byte_identical_to_no_page_context():
    # A rich page_context under flag-OFF must assemble to EXACTLY the same messages — and the
    # same final prompt bytes — as a turn that carried no page_context at all.
    control = _assemble(None, flag=False)
    with_pc_off = _assemble(_PC, flag=False)
    assert with_pc_off == control
    assert _messages_to_prompt(with_pc_off) == _messages_to_prompt(control)


def test_flag_on_prompt_adds_exactly_one_front_adjacent_card():
    control = _assemble(None, flag=True)
    with_pc_on = _assemble(_PC, flag=True)
    # Exactly one extra message, inserted front-adjacent (ahead of the project card).
    assert len(with_pc_on) == len(control) + 1
    assert with_pc_on[0] == page_context_card(_PC, enabled=True)
    assert with_pc_on[1]["content"].startswith("Project context:")
    # And the snapshot text reaches the assembled prompt.
    assert "Screen: teams" in _messages_to_prompt(with_pc_on)
