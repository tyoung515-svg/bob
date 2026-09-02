"""Test-pipe — the honesty fence (SPEC §7, HARD / non-negotiable).

The fence guard test proper (across the full pipe) lives in ``test_tp_e2e``; these
unit tests prove the enforcement primitive catches a leak, is not trivially green
(a deliberately injected hidden string RAISES), and never false-positives on the
legitimate visible material.
"""
from __future__ import annotations

import pytest

from core.testpipe.fence import (
    FenceError,
    PromptRecorder,
    make_fenced_send,
    scan_text,
)
from core.testpipe.fixtures import fixture_items


def _item():
    return fixture_items()[0]  # fx_add: hidden includes 'assert add(-1, 1) == 0'


def test_scan_text_flags_a_hidden_payload():
    it = _item()
    leaked = "here is the impl\nassert add(-1, 1) == 0\nplease pass"
    assert scan_text(leaked, it) == ["assert add(-1, 1) == 0"]


def test_scan_text_no_false_positive_on_visible():
    it = _item()
    # A legitimate prompt that calls the entry point + shows the VISIBLE test only.
    prompt = it.prompt + "\n" + "\n".join(it.visible_tests)
    assert scan_text(prompt, it) == []


def test_scan_matches_whitespace_reformatted_leak():
    it = _item()
    reformatted = "assert   add(-1,1)==0"   # spacing differs from the payload
    assert scan_text(reformatted, it) == ["assert add(-1, 1) == 0"]


async def test_fenced_send_raises_on_a_hidden_leak():
    # NOT trivially green: a stage that tries to splice a hidden test into a prompt
    # fails LOUD at the wire (the whole point of §7.3).
    it = _item()

    async def _raw(messages, backend, model=None):
        return "ok"

    fenced = make_fenced_send(_raw, it, stage="worker")
    with pytest.raises(FenceError):
        await fenced(
            [{"role": "user", "content": "cheat sheet: assert add(-1, 1) == 0"}],
            "deepseek_v4_flash",
        )


async def test_fence_catches_payload_split_across_messages():
    # audit r1 hardening: a payload split across system+user messages (the model gets
    # the concatenation) must still trip the fence.
    it = _item()  # hidden includes 'assert add(-1, 1) == 0'

    async def _raw(messages, backend, model=None):
        return "ok"

    fenced = make_fenced_send(_raw, it, stage="worker")
    with pytest.raises(FenceError):
        await fenced(
            [{"role": "system", "content": "hint: assert add(-1, "},
             {"role": "user", "content": "1) == 0 -- make it pass"}],
            "deepseek_v4_flash",
        )


async def test_fenced_send_passes_and_records_clean_prompt():
    it = _item()
    rec = PromptRecorder()

    async def _raw(messages, backend, model=None):
        return "def add(a, b):\n    return a + b"

    fenced = make_fenced_send(_raw, it, recorder=rec, stage="worker")
    out = await fenced([{"role": "user", "content": "\n".join(it.visible_tests)}],
                       "deepseek_v4_flash")
    assert out.startswith("def add")
    assert rec.leaks_for(it) == []       # the recorder independently confirms no leak
    assert rec.prompts and rec.prompts[0]["stage"] == "worker"
