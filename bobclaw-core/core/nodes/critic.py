"""
BoBClaw — Worker fan-out critic (handoff 008, LKS v3.1 rule 16).

`run_critic` is called by `worker_node` after the producer call succeeds.
It returns a verdict tuple (`verdict`, `reasons`) where verdict is one of
"approve" | "flag" | "reject". Critic failures (timeout, parse error, 429)
return ("none", ["critic_unavailable: <reason>"]).

Bounded action space + JSON-schema-in-prompt — no function-calling required.
Works on any backend that emits structured text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

from core.config import CRITIC_TIMEOUT_SECONDS
from core.nodes.execute import _send_to_backend

logger = logging.getLogger(__name__)

CRITIC_DEFAULT_PROMPT_TEMPLATE: str = """You are reviewing a worker's output for a subtask.

Subtask: {subtask_text}

Worker output: {worker_output}

Evaluate the output. Respond with a JSON object on a single line:
{{"verdict": "approve" | "flag" | "reject", "reasons": ["short reason 1", "short reason 2"]}}

- "approve": output is correct, complete, and usable as-is
- "flag": output is usable but has a concern worth surfacing (factual uncertainty, scope drift, partial completion)
- "reject": output is wrong, fabricated, or unusable

Respond with the JSON object and nothing else."""


class CriticVerdict(BaseModel):
    verdict: str = Field(pattern=r"^(approve|flag|reject)$")
    reasons: list[str]


def extract_json(text: str) -> Optional[dict]:
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()

    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


def parse_verdict(raw: str) -> tuple[str, list[str]]:
    data = extract_json(raw)
    if data is None:
        return ("none", ["critic_unavailable: parse_error: could not extract JSON"])
    try:
        v = CriticVerdict.model_validate(data)
        return (v.verdict, v.reasons)
    except Exception as exc:
        return ("none", [f"critic_unavailable: parse_error: {exc}"])


async def run_critic(
    subtask_text: str,
    worker_output: str,
    critic_backend: str,
    prompt_template: Optional[str] = None,
) -> tuple[str, list[str]]:
    from core.config import config

    template = prompt_template or CRITIC_DEFAULT_PROMPT_TEMPLATE
    prompt = template.format(subtask_text=subtask_text, worker_output=worker_output)
    messages = [
        {"role": "system", "content": "You are a critic evaluating worker output."},
        {"role": "user", "content": prompt},
    ]

    # Primary critic, then a healthy stand-in on a HARD failure (timeout / HTTP error —
    # e.g. Z.AI GLM's balance-exhausted 429). A parse failure is NOT a backend failure,
    # so it does not trigger the stand-in (the backend answered, just badly).
    backends = [critic_backend]
    fallback = config.CRITIC_FALLBACK_BACKEND
    if fallback and fallback != critic_backend:
        backends.append(fallback)

    last_reason = "critic_unavailable: no critic backend"
    for idx, backend in enumerate(backends):
        try:
            raw = await asyncio.wait_for(
                _send_to_backend(messages, backend),
                timeout=CRITIC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            last_reason = f"critic_unavailable: timeout {CRITIC_TIMEOUT_SECONDS}s"
            continue
        except Exception as exc:
            logger.warning("Critic call failed for backend=%r: %s", backend, exc)
            last_reason = f"critic_unavailable: {type(exc).__name__}: {exc}"
            continue
        verdict, reasons = parse_verdict(raw)
        if idx > 0 and verdict != "none":
            # Mark that the stand-in produced this verdict (the primary was down).
            reasons = [f"critic_standin={backend}", *reasons]
        return (verdict, reasons)
    return ("none", [last_reason])
