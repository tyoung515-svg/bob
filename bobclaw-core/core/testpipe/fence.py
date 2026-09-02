"""Test-pipe — the honesty fence (SPEC §7, HARD / non-negotiable).

The single most important invariant of the whole pipe (SPEC §7.3): **no hidden-test
payload string ever appears in any prompt sent to a model.** This module is the
ENFORCEMENT seam — every model call in the build/council spines goes through a
:func:`make_fenced_send` wrapper that (a) scans the outgoing prompt for any hidden
payload and RAISES :class:`FenceError` on a leak, and (b) records the prompt so a
test can independently assert the recorder saw no hidden string across
front/worker/audit/repair (SPEC §7.3 fence guard test).

Enforcing at the send seam (not just testing after the fact) means a future stage
that accidentally splices a hidden test fails LOUD at the wire, by construction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from core.testpipe.types import TestItem

SendFn = Callable[..., Awaitable[str]]


class FenceError(RuntimeError):
    """Raised when a hidden-test payload is about to cross into a model prompt.

    A hard stop: contamination would silently invalidate the whole benchmark, so we
    fail the run rather than leak (SPEC §7 — non-negotiable)."""


def _norm(text: str) -> str:
    """Whitespace-insensitive normalization so a reformatted leak is still caught."""
    return re.sub(r"\s+", "", text or "")


def hidden_payloads(item: TestItem) -> tuple[str, ...]:
    """The hidden-test strings that must never enter a prompt for *item*."""
    return tuple(t for t in item.hidden_tests if t and t.strip())


def scan_text(text: str, item: TestItem) -> list[str]:
    """Return the list of hidden payloads found leaked in *text* (empty = clean).

    Matches on the WHOLE normalized assertion (not a shared fragment like the
    function name), so a visible prompt that legitimately calls ``entry_point`` is
    not a false positive — only a full hidden assertion string trips it."""
    ntext = _norm(text)
    return [p for p in hidden_payloads(item) if _norm(p) and _norm(p) in ntext]


def scan_messages(messages: list[dict], item: TestItem) -> list[str]:
    """All hidden payloads leaked across a messages list (front/worker/audit/repair
    all pass a ``[{role, content}]`` list to the send seam).

    Scans each message AND the concatenation of all message contents — the latter
    catches a payload deliberately/accidentally SPLIT across the system+user messages
    (which the model receives as one context), a gap per-message scanning alone would
    miss (audit r1). Conservative by design: a boundary that spuriously forms a payload
    trips the fence, which fails loud (the safe direction for the HARD invariant)."""
    leaks: list[str] = []
    contents = [
        (m.get("content", "") if isinstance(m, dict) else str(m)) for m in (messages or [])
    ]
    for content in contents:
        leaks.extend(scan_text(content, item))
    for p in scan_text("".join(contents), item):
        if p not in leaks:
            leaks.append(p)
    return leaks


def assert_no_leak(messages: list[dict], item: TestItem, *, stage: str = "?") -> None:
    """Raise :class:`FenceError` if any message would leak a hidden payload."""
    leaks = scan_messages(messages, item)
    if leaks:
        raise FenceError(
            f"honesty fence tripped at stage {stage!r} for item {item.id!r}: "
            f"{len(leaks)} hidden-test payload(s) about to enter a model prompt: "
            f"{leaks[0]!r}"
        )


@dataclass
class PromptRecorder:
    """Records every prompt the pipe sends, tagged by stage, for the fence guard
    test to inspect independently of the inline enforcement."""

    prompts: list[dict] = field(default_factory=list)

    def record(self, stage: str, messages: list[dict], backend: str) -> None:
        self.prompts.append({"stage": stage, "backend": backend,
                             "messages": [dict(m) for m in (messages or [])]})

    def all_text(self) -> str:
        return "\n".join(
            m.get("content", "") for p in self.prompts for m in p["messages"]
        )

    def leaks_for(self, item: TestItem) -> list[str]:
        """Hidden payloads that appear ANYWHERE in the recorded prompts (the fence
        guard assertion, SPEC §7.3)."""
        return scan_text(self.all_text(), item)


def make_fenced_send(
    inner: SendFn,
    item: TestItem,
    *,
    recorder: Optional[PromptRecorder] = None,
    stage: str = "?",
) -> SendFn:
    """Wrap a send seam so every call is fence-checked + recorded before dispatch.

    Matches ``_send_to_backend(messages, backend, model=None)`` so it drops in
    wherever the pipe would call the real seam. Enforcement order: assert-no-leak
    (raise on a hidden payload) → record → delegate. The ``stage`` tag flows into
    both the :class:`FenceError` message and the recorder."""

    async def _send(messages, backend, model=None):
        assert_no_leak(messages, item, stage=stage)
        if recorder is not None:
            recorder.record(stage, messages, backend)
        if model is not None:
            return await inner(messages, backend, model)
        return await inner(messages, backend)

    return _send
