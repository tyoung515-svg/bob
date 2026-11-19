"""Fit the prior-conversation block to a model's context budget without silent loss.

This is the safety net, not the compaction strategy. The old behaviour dropped the oldest messages
with no trace and, if the single newest message was bigger than the cap, dropped EVERYTHING. Here:

  * the opening exchange (usually the goal, constraints and definitions) is protected;
  * the newest messages are kept contiguously from the newest backwards;
  * the newest message is never dropped (truncated in place if it alone exceeds the budget);
  * whatever sits between the two is announced in the prompt itself and reported as metadata.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from core.context.windows import (
    CHARS_PER_TOKEN,
    fit_enabled,
    limits_for,
    prompt_char_cap,
)

logger = logging.getLogger(__name__)

PRIOR_HEADER = "Prior conversation:\n"
_ROLE_OVERHEAD_TOKENS = 4
_TURN_BOUNDARY = re.compile(r"\n(?=(?:user|assistant|system|tool): )")

# The opening exchange is kept (shrunk if needed) while there is anything to drop in the middle.
HEAD_PROTECT_MESSAGES = 2
HEAD_MAX_FRACTION = 0.15


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _message_tokens(role: str, content: str) -> int:
    return estimate_tokens(f"{role}: {content}") + _ROLE_OVERHEAD_TOKENS


def _truncate_middle(text: str, max_chars: int) -> str:
    """Keep the head and tail of *text*, say how much was cut from the middle."""
    if len(text) <= max_chars:
        return text
    cut = len(text) - max_chars
    marker = f"\n[... {cut} characters omitted from this message to fit the context budget ...]\n"
    keep = max(max_chars - len(marker), 200)
    head = keep * 2 // 5
    return text[:head] + marker + text[len(text) - (keep - head):]


@dataclass
class FitResult:
    # The protected opening messages (``head_count`` of them) followed by the contiguous newest tail.
    kept: list[dict] = field(default_factory=list)
    head_count: int = 0
    omitted: list[dict] = field(default_factory=list)
    truncated_ids: list = field(default_factory=list)
    tokens_before: int = 0
    tokens_after: int = 0
    budget_tokens: int = 0
    marker: Optional[str] = None
    # Verified claims standing in for the oldest omitted messages (see core.context.assembler), and what was used.
    claims_block: Optional[str] = None
    claims_info: dict = field(default_factory=dict)


def _omission_marker(omitted: list[dict], budget_tokens: int, after_head: bool) -> str:
    first, last = omitted[0], omitted[-1]
    span = ""
    if first.get("created_at") and last.get("created_at"):
        span = f" from {first['created_at']} to {last['created_at']}"
    ids = ""
    if first.get("id") is not None and last.get("id") is not None:
        ids = f" (message ids {first['id']}..{last['id']})"
    where = "between the opening exchange and the recent messages" if after_head else "before the recent messages"
    return (
        f"[context note: {len(omitted)} message(s){span}{ids} {where} were left out of this prompt to "
        f"fit a context budget of about {budget_tokens} tokens. They are still stored in the "
        "conversation log (if you have the conv_expand / conv_search tools, use them to read the omitted "
        "messages); do not assume nothing was said in between.]"
    )


def fit_history(history: list[dict], budget_tokens: int) -> FitResult:
    """Fit *history* (oldest-first) to *budget_tokens*: opening exchange + newest contiguous tail."""
    items = [
        m for m in (history or [])
        if isinstance(m, dict) and m.get("content") not in (None, "")
    ]
    result = FitResult(budget_tokens=budget_tokens)
    costs = [_message_tokens(str(m.get("role") or "user"), str(m["content"])) for m in items]
    result.tokens_before = sum(costs)
    if result.tokens_before <= budget_tokens:
        result.kept, result.tokens_after = list(items), result.tokens_before
        return result

    # Protect the opening exchange (only meaningful when there is a middle to drop).
    head: list[dict] = []
    head_cost = 0
    if len(items) > HEAD_PROTECT_MESSAGES + 1:
        per_message_chars = max(
            int(budget_tokens * HEAD_MAX_FRACTION * CHARS_PER_TOKEN / HEAD_PROTECT_MESSAGES) - 40, 200
        )
        for m in items[:HEAD_PROTECT_MESSAGES]:
            role, content = str(m.get("role") or "user"), str(m["content"])
            if len(content) > per_message_chars:
                content = _truncate_middle(content, per_message_chars)
                result.truncated_ids.append(m.get("id"))
            head.append({**m, "content": content})
            head_cost += _message_tokens(role, content)

    rest = items[len(head):]
    remaining = budget_tokens - head_cost
    tail_newest_first: list[dict] = []
    used = 0
    omitted: list[dict] = []
    for i in range(len(rest) - 1, -1, -1):
        m = rest[i]
        role, content = str(m.get("role") or "user"), str(m["content"])
        cost = _message_tokens(role, content)
        if not tail_newest_first:
            # The latest message is never dropped; shrink it in place if it alone is over budget.
            if cost > remaining:
                content = _truncate_middle(content, max(int(remaining * CHARS_PER_TOKEN) - 200, 400))
                cost = _message_tokens(role, content)
                result.truncated_ids.append(m.get("id"))
            tail_newest_first.append({**m, "content": content})
            used += cost
            continue
        if used + cost > remaining:
            omitted = rest[: i + 1]
            break
        tail_newest_first.append(m)
        used += cost

    result.kept = head + list(reversed(tail_newest_first))
    result.head_count = len(head)
    result.omitted = omitted
    result.tokens_after = head_cost + used
    if omitted:
        result.marker = _omission_marker(omitted, budget_tokens, after_head=bool(head))
    return result


def render_prior_conversation(fit: FitResult) -> str:
    """The system-message text; same ``role: content`` lines as before, plus an omission note in place."""
    lines = [f"{m.get('role') or 'user'}: {m['content']}" for m in fit.kept]
    for offset, note in enumerate(x for x in (fit.claims_block, fit.marker) if x):
        lines.insert(fit.head_count + offset, note)
    return PRIOR_HEADER + "\n".join(lines)


def trim_prior_conversation_text(text: str, max_chars: int) -> str:
    """String-level fallback: cut the MIDDLE of a rendered block down to *max_chars*, keeping the opening."""
    if len(text) <= max_chars:
        return text
    body = text[len(PRIOR_HEADER):] if text.startswith(PRIOR_HEADER) else text
    note = (
        "[context note: part of this conversation was left out to fit this backend's "
        f"prompt limit (about {max_chars} characters).]\n"
    )
    room = max_chars - len(PRIOR_HEADER) - len(note)
    if room <= 0:
        return PRIOR_HEADER + note.rstrip()
    boundaries = [m.start() for m in _TURN_BOUNDARY.finditer(body)]
    head = ""
    head_end = 0
    if len(boundaries) >= 2:
        head_end = boundaries[1]
        head = body[:head_end]
        if len(head) > room * 0.15:
            head = _truncate_middle(head, int(room * 0.15))
        head += "\n"
    tail_room = room - len(head)
    cut = max(len(body) - tail_room, head_end + 1)
    boundary = _TURN_BOUNDARY.search(body, cut)
    start = boundary.end() if boundary else cut
    return PRIOR_HEADER + head + note + body[start:]


def enforce_backend_limits(messages: list[dict], backend: Optional[str]) -> list[dict]:
    """Last line of defence at call time, when the FINAL backend is known.

    History is fitted before routing, against the face's preferred backend. If routing lands on a
    backend with a hard prompt cap (a configured-window local model, an argv-limited CLI) the
    prior-conversation block is trimmed here so the call degrades gracefully instead of failing.
    """
    if not fit_enabled():
        return messages
    cap = prompt_char_cap(limits_for(backend))
    if cap is None:
        return messages
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= cap:
        return messages
    for idx, m in enumerate(messages):
        content = str(m.get("content") or "")
        if m.get("role") == "system" and content.startswith(PRIOR_HEADER):
            target = max(len(content) - (total - cap), 2_000)
            out = list(messages)
            out[idx] = {**m, "content": trim_prior_conversation_text(content, target)}
            logger.warning(
                "context: trimmed prior conversation for backend=%s (%d -> %d chars, prompt cap %d)",
                backend, len(content), len(out[idx]["content"]), cap,
            )
            return out
    logger.warning(
        "context: prompt for backend=%s is %d chars, over its %d cap, with no prior-conversation block to trim",
        backend, total, cap,
    )
    return messages
