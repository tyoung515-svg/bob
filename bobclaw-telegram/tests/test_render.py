"""bobclaw-telegram — render.py tests (pure, no I/O)."""
from __future__ import annotations

import pytest

from bobclaw_telegram.render import (
    TELEGRAM_LIMIT,
    EditThrottle,
    chunk_text,
    markdown_rejected,
    md_v2,
)


def _fence_count(chunk: str) -> int:
    return sum(1 for line in chunk.split("\n") if line.lstrip().startswith("```"))


# ── chunking ──────────────────────────────────────────────────────────────

def test_short_text_single_chunk():
    assert chunk_text("hello") == ["hello"]
    assert chunk_text("x" * TELEGRAM_LIMIT) == ["x" * TELEGRAM_LIMIT]


def test_long_plain_text_chunks_under_limit_and_reassemble():
    text = "\n".join(f"line {i} " + "y" * 50 for i in range(500))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    assert "\n".join(chunks) == text


def test_chunk_respects_custom_limit():
    chunks = chunk_text("aaaa\nbbbb\ncccc", limit=5)
    assert chunks == ["aaaa", "bbbb", "cccc"]


def test_overlong_single_line_hard_split():
    text = "z" * (TELEGRAM_LIMIT * 2 + 10)
    chunks = chunk_text(text)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    assert "".join(chunks) == text


# ── fence-aware splits ────────────────────────────────────────────────────

def test_fence_split_closes_and_reopens_with_language_tag():
    body = "\n".join(f"x{i} = {i}" for i in range(1200))  # ~9k chars
    text = f"```python\n{body}\n```"
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    # Every chunk is fence-balanced (no chunk ends inside a fence).
    for c in chunks:
        assert _fence_count(c) % 2 == 0, c[:80]
    # First chunk closed the fence; the next reopens it with the language tag.
    assert chunks[0].endswith("```")
    assert chunks[1].startswith("```python")


def test_fence_content_preserved_across_split():
    body = "\n".join(f"row-{i}-" + "q" * 30 for i in range(800))
    text = f"```\n{body}\n```"
    chunks = chunk_text(text)
    # Strip the fence markers the splitter added; the code body is intact.
    reassembled = "\n".join(chunks)
    for line in body.split("\n"):
        assert line in reassembled
    assert reassembled.count("row-") == 800


def test_hard_split_inside_fence_reopens():
    text = "```python\n" + "w" * (TELEGRAM_LIMIT * 2) + "\n```"
    chunks = chunk_text(text)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    for c in chunks:
        assert _fence_count(c) % 2 == 0
    assert chunks[1].startswith("```python")


def test_text_outside_fence_not_fence_closed():
    text = ("intro " + "a" * 100 + "\n") * 300
    chunks = chunk_text(text)
    assert all(_fence_count(c) == 0 for c in chunks)


def test_chunk_text_rejects_tiny_limit():
    with pytest.raises(ValueError):
        chunk_text("x" * 100, limit=4)


# ── MarkdownV2 degrade ───────────────────────────────────────────────────

def test_md_v2_escapes_reserved_chars():
    assert md_v2("a*b_c[d](e)~`#+-.!") == "a\\*b\\_c\\[d\\]\\(e\\)\\~\\`\\#\\+\\-\\.\\!"


def test_md_v2_keeps_fence_lines_and_escapes_code_minimally():
    out = md_v2("```python\nx = `a` \\ b*1\n```")
    lines = out.split("\n")
    assert lines[0] == "```python"
    assert lines[2] == "```"
    # Inside the fence only backtick/backslash are escaped; '*' stays literal.
    assert lines[1] == "x = \\`a\\` \\\\ b*1"


def test_md_v2_plain_text_unchanged():
    assert md_v2("hello world") == "hello world"


# ── plain-text fallback decision ──────────────────────────────────────────

def test_markdown_rejected_true_for_bad_request():
    from telegram.error import BadRequest

    assert markdown_rejected(BadRequest("Can't parse entities"))


def test_markdown_rejected_false_for_other_errors():
    assert not markdown_rejected(RuntimeError("boom"))
    assert not markdown_rejected(TimeoutError())


# ── edit-in-place throttle ────────────────────────────────────────────────

def test_edit_throttle_allows_first_then_gates():
    t = EditThrottle(0.8)
    assert t.due(100.0)          # first edit always goes out
    assert not t.due(100.5)      # inside the 0.8s window
    assert t.due(100.9)          # window elapsed (0.9 avoids float edge at 0.8)


def test_edit_throttle_reset_rearms():
    t = EditThrottle(0.8)
    assert t.due(0.0)
    t.reset()
    assert t.due(0.1)
