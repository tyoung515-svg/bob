"""bobclaw-telegram — approvals notify-only relay tests (pure + fake fetch/send).

Covers the notify formatting, the new-ids-only diffing, and the fail-open
poll loop. No network, no PTB.
"""
from __future__ import annotations

import asyncio

from bobclaw_telegram.approvals import (
    ApprovalNotifier,
    new_pending,
    notify_text,
)

CHATS = [111, 222]


def _item(approval_id, kind="task_approval", details=None):
    item = {"id": approval_id, "action_type": kind, "status": "pending"}
    if details is not None:
        item["details"] = details
    return item


def _run(coro):
    return asyncio.run(coro)


# ── formatting ──────────────────────────────────────────────────────────────

def test_notify_text_with_details():
    text = notify_text(_item("a1", "cc_edit", {"path": "core/x.py", "lines": 12}))
    assert text == (
        "⏳ Approval pending: cc_edit — path=core/x.py, lines=12 — decide in the TUI"
    )


def test_notify_text_without_details_omits_summary_segment():
    text = notify_text(_item("a1", "task_approval"))
    assert text == "⏳ Approval pending: task_approval — decide in the TUI"


def test_notify_text_unknown_kind_still_renders():
    text = notify_text({"id": "a1"})
    assert text == "⏳ Approval pending: ? — decide in the TUI"


def test_notify_text_many_detail_keys_get_ellipsis_marker():
    details = {f"k{i}": "v" for i in range(5)}
    text = notify_text(_item("a1", "cc_edit", details))
    assert "k0=v, k1=v, k2=v, …" in text
    assert "k3" not in text


def test_notify_text_summary_capped_at_max_len():
    details = {f"key{i}": "v" * 40 for i in range(5)}
    text = notify_text(_item("a1", "cc_edit", details))
    head = text.split(" — decide in the TUI")[0]
    assert len(head) <= len("⏳ Approval pending: cc_edit — ") + 60


# ── diffing ─────────────────────────────────────────────────────────────────

def test_new_pending_returns_only_unseen_ids():
    items = [_item("a"), _item("b"), _item("c")]
    assert [i["id"] for i in new_pending(items, {"a"})] == ["b", "c"]


def test_new_pending_skips_malformed_items():
    items = [_item("a"), {"action_type": "x"}, "garbage", None]
    assert [i["id"] for i in new_pending(items, set())] == ["a"]


# ── notifier poll loop ──────────────────────────────────────────────────────

class FakeFetch:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self._pages.pop(0) if self._pages else []


class FakeSend:
    def __init__(self, fail_on: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self._fail_on = fail_on or set()

    async def __call__(self, chat_id, text):
        if chat_id in self._fail_on:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


def test_first_poll_relays_everything_currently_pending():
    fetch = FakeFetch([[_item("a"), _item("b")]])
    send = FakeSend()
    notifier = ApprovalNotifier(fetch, send, CHATS)

    new = _run(notifier.poll_once())

    assert [i["id"] for i in new] == ["a", "b"]
    # one message per new approval per allowlisted chat
    assert len(send.sent) == 4
    assert {chat for chat, _ in send.sent} == set(CHATS)
    assert all(t.startswith("⏳ Approval pending:") for _, t in send.sent)


def test_second_poll_relays_only_new_ids():
    fetch = FakeFetch([
        [_item("a"), _item("b")],
        [_item("a"), _item("b"), _item("c")],
    ])
    send = FakeSend()
    notifier = ApprovalNotifier(fetch, send, CHATS)

    async def main():
        await notifier.poll_once()
        return await notifier.poll_once()

    new = _run(main())
    assert [i["id"] for i in new] == ["c"]
    assert len(send.sent) == 4 + 2  # a,b on poll 1; only c on poll 2


def test_decided_approvals_are_not_relayed_again():
    fetch = FakeFetch([
        [_item("a"), _item("b")],
        [_item("b")],          # a decided in the TUI
        [_item("b")],          # nothing changed
    ])
    send = FakeSend()
    notifier = ApprovalNotifier(fetch, send, CHATS)

    async def main():
        await notifier.poll_once()
        assert await notifier.poll_once() == []
        return await notifier.poll_once()

    assert _run(main()) == []
    assert len(send.sent) == 4  # only the first poll's a,b


def test_fetch_failure_is_fail_open():
    async def boom():
        raise RuntimeError("gateway down")

    send = FakeSend()
    notifier = ApprovalNotifier(boom, send, CHATS)
    assert _run(notifier.poll_once()) == []
    assert send.sent == []


def test_send_failure_to_one_chat_does_not_stop_the_rest():
    fetch = FakeFetch([[_item("a")]])
    send = FakeSend(fail_on={111})
    notifier = ApprovalNotifier(fetch, send, CHATS)

    new = _run(notifier.poll_once())
    assert [i["id"] for i in new] == ["a"]
    assert send.sent == [(222, notify_text(_item("a")))]


def test_seen_ids_not_advanced_on_fetch_failure():
    pages = FakeFetch([[_item("a")]])
    send = FakeSend()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return await pages()

    notifier = ApprovalNotifier(flaky, send, CHATS)

    async def main():
        await notifier.poll_once()       # fails
        return await notifier.poll_once()  # a is still "new"

    new = _run(main())
    assert [i["id"] for i in new] == ["a"]
