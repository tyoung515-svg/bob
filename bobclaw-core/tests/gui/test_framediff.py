"""Tests for core.gui.framediff (pure frame-diff primitives)."""
from __future__ import annotations

import hashlib

from core.gui import a11y_contains, a11y_index, frame_diff, frame_signature, hash_bytes
from conftest import frame, node


def test_hash_bytes_sentinel_for_none_and_empty():
    sentinel = hashlib.sha256(b"").hexdigest()
    assert hash_bytes(None) == sentinel
    assert hash_bytes(b"") == sentinel
    assert hash_bytes(b"x") == hashlib.sha256(b"x").hexdigest()
    assert hash_bytes(b"x") != sentinel


def test_frame_signature_order_independent_but_content_sensitive():
    a = node("button", "ok")
    b = node("button", "cancel")
    f1 = frame("img", a, b)
    f2 = frame("img", b, a)  # same nodes, reordered
    assert frame_signature(f1) == frame_signature(f2)
    # value change shifts the signature
    f3 = frame("img", node("button", "ok", value="x"), b)
    assert frame_signature(f3) != frame_signature(f1)
    # image hash change shifts the signature
    assert frame_signature(frame("img2", a, b)) != frame_signature(f1)


def test_frame_signature_ignores_seq():
    f1 = frame("img", node("x"), seq=1)
    f2 = frame("img", node("x"), seq=99)
    assert frame_signature(f1) == frame_signature(f2)


def test_frame_signature_includes_node_id_and_size():
    # audit fix: a node_id change is a real change (consistent with frame_diff's keying)
    f1 = frame("img", node("button", "ok", node_id="b1"))
    f2 = frame("img", node("button", "ok", node_id="b2"))
    assert frame_signature(f1) != frame_signature(f2)
    # a resize is also a change
    assert frame_signature(frame("img", node("x"), size=(10, 10))) != \
        frame_signature(frame("img", node("x"), size=(20, 20)))


def test_frame_signature_delimiter_safe():
    # audit fix: a '|' inside a field must not forge a node boundary -> no collision
    f1 = frame("img", node("a|b", "c"))
    f2 = frame("img", node("a", "b|c"))
    assert frame_signature(f1) != frame_signature(f2)


def test_a11y_index_keys_by_node_id_else_role_name():
    f = frame("img", node("button", "ok", node_id="b1"), node("label", "title"))
    idx = a11y_index(f)
    assert set(idx) == {"b1", "label:title"}


def test_a11y_index_last_wins_on_dup_key():
    f = frame("img", node("button", "ok", value="first"), node("button", "ok", value="second"))
    idx = a11y_index(f)
    assert idx["button:ok"].value == "second"


def test_frame_diff_first_frame_all_changed():
    d = frame_diff(None, frame("img", node("button", "ok")))
    assert d.changed and d.pixel_changed and d.a11y_changed
    assert d.added == ("button:ok",)
    assert d.removed == ()


def test_frame_diff_pixel_only():
    f1 = frame("a", node("button", "ok"))
    f2 = frame("b", node("button", "ok"))
    d = frame_diff(f1, f2)
    assert d.pixel_changed and not d.a11y_changed and d.changed
    assert d.added == () and d.removed == () and not d.text_changed


def test_frame_diff_a11y_add_remove_text():
    f1 = frame("img", node("button", "ok", value="v1"))
    f2 = frame("img", node("button", "ok", value="v2"), node("label", "new"))
    d = frame_diff(f1, f2)
    assert d.added == ("label:new",)
    assert d.removed == ()
    assert d.text_changed
    assert d.a11y_changed and d.changed and not d.pixel_changed


def test_frame_diff_no_change():
    f = frame("img", node("button", "ok"))
    d = frame_diff(f, frame("img", node("button", "ok")))
    assert not d.changed and not d.pixel_changed and not d.a11y_changed


def test_a11y_contains_filters_and_empty():
    f = frame("img", node("button", "Save", value="enabled", node_id="b1"))
    assert a11y_contains(f, node_id="b1")
    assert a11y_contains(f, name="Save")
    assert a11y_contains(f, value_substr="nabl")
    assert a11y_contains(f, node_id="b1", value_substr="enab")
    assert not a11y_contains(f, node_id="b1", name="Other")  # must match ALL filters
    assert not a11y_contains(f)  # empty filters never trivially pass
    assert not a11y_contains(f, name="Missing")
