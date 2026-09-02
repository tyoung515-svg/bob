"""bobclaw-telegram — outbound rendering helpers (pure, no I/O).

- ``chunk_text``: fence-aware 4096-char chunking. A chunk is never left ending
  inside a code fence — a mid-fence split closes the fence at the end of the
  chunk and reopens it (with its language tag) at the start of the next.
- ``md_v2``: MarkdownV2 degrade helper — escape the reserved characters so a
  model's markdown survives Telegram's strict MarkdownV2 parser without 400s.
- ``markdown_rejected``: the plain-text fallback decision — any Telegram
  BadRequest on a markdown send means resend as plain text (never lose the
  reply).
- ``EditThrottle``: edit-in-place streaming policy (~1 edit per 0.8s).
"""
from __future__ import annotations

TELEGRAM_LIMIT = 4096
EDIT_INTERVAL = 0.8

# Reserved characters in Telegram MarkdownV2 (must be backslash-escaped
# outside code blocks).
_MD_V2_RESERVED = set("_*[]()~`>#+-=|{}.!")
# Inside a pre/code block only backslash and backtick need escaping.
_MD_V2_CODE_RESERVED = set("\\`")


def _is_fence_line(line: str) -> bool:
    return line.lstrip().startswith("```")


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split *text* into chunks of at most *limit* chars, fence-aware.

    Splits on line boundaries when possible. A split that lands inside a code
    fence closes the fence (```` ``` ````) at the end of the chunk and reopens
    it (```` ```lang ````) at the start of the next, so every chunk renders on
    its own. Overlong single lines are hard-split at the limit.
    """
    if limit < 5:
        raise ValueError("limit too small for fence close/reopen overhead")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    cur = ""
    in_fence = False
    fence_tag = "```"

    def flush() -> None:
        nonlocal cur
        if in_fence:
            cur = cur + "\n```" if cur else "```"
        chunks.append(cur)
        cur = ""

    for line in text.split("\n"):
        while True:
            sep = "\n" if cur else ""
            reserve = 4 if in_fence else 0  # room for a closing "\n```"
            if len(cur) + len(sep) + len(line) + reserve <= limit:
                cur = cur + sep + line
                break
            tag_only = in_fence and cur == fence_tag
            if cur and not tag_only:
                # Flush the current chunk; a mid-fence flush reopens the
                # fence in the next chunk.
                reopen = in_fence
                flush()
                if reopen:
                    cur = fence_tag
                continue
            # Line overflows even an empty/freshly-reopened chunk:
            # hard-split it around whatever cur already holds.
            room = limit - len(cur) - len(sep) - reserve
            if room <= 0:
                # Pathological (huge fence tag + tiny limit): flush the tag
                # alone WITHOUT reopening so the loop always terminates.
                flush()
                in_fence = False
                continue
            head, line = line[:room], line[room:]
            cur = cur + sep + head
            reopen = in_fence
            flush()
            if reopen:
                cur = fence_tag
        if _is_fence_line(line):
            if in_fence:
                in_fence = False
            else:
                in_fence = True
                fence_tag = line.strip()

    if cur:
        chunks.append(cur)
    return chunks


def md_v2(text: str) -> str:
    """Degrade arbitrary markdown to Telegram-safe MarkdownV2.

    Escapes every reserved character outside code fences; inside fences only
    backslash and backtick are escaped (Telegram's pre-block rule). Fence
    marker lines pass through untouched so code blocks still render.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _is_fence_line(line):
            in_fence = not in_fence
            out.append(line)
            continue
        reserved = _MD_V2_CODE_RESERVED if in_fence else _MD_V2_RESERVED
        out.append("".join("\\" + c if c in reserved else c for c in line))
    return "\n".join(out)


def markdown_rejected(exc: BaseException) -> bool:
    """Plain-text fallback decision: any Telegram BadRequest on a markdown send.

    Telegram rejects a MarkdownV2 message with BadRequest ("can't parse
    entities" and friends); the caller resends the same text with no
    parse_mode so the reply is never lost. Anything else (network errors,
    rate limits) is NOT a markup problem and must propagate.
    """
    try:
        from telegram.error import BadRequest
    except ImportError:  # pragma: no cover - PTB is a declared dependency
        return type(exc).__name__ == "BadRequest"
    return isinstance(exc, BadRequest)


class EditThrottle:
    """Edit-in-place streaming policy: at most one edit per *interval* seconds.

    Pure — the caller injects the clock (``time.monotonic`` in production) so
    tests never sleep. ``due()`` returns True and re-arms when an edit may go
    out; the final flush after a stream ends is always allowed regardless.
    """

    def __init__(self, interval: float = EDIT_INTERVAL) -> None:
        self.interval = interval
        self._last: float | None = None

    def due(self, now: float) -> bool:
        if self._last is None or now - self._last >= self.interval:
            self._last = now
            return True
        return False

    def reset(self) -> None:
        self._last = None
