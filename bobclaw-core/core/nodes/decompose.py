"""
BoBClaw Core — Task decomposition node

Simple heuristic: short, single-line tasks pass through unchanged.
Complex tasks are decomposed by calling the active LLM backend.
The LLM call is routed through a module-level `_call_llm` reference
so tests can monkeypatch it without touching the HTTP stack.

Backend selection: when the state carries an explicit (non-local) backend —
e.g. a gateway/CLI request that asked for ``deepseek_v4_flash`` — the
decomposition call honours it via the same ``execute._send_to_backend``
transport the rest of the graph uses for cloud backends.  When no backend is
specified (``"local"`` / empty) the call falls back to local-model discovery,
preserving the original default behaviour.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from core.graph import AgentState

logger = logging.getLogger(__name__)

# ─── Complexity heuristics ────────────────────────────────────────────────────

_COMPLEX_THRESHOLD = 150  # characters

_COMPLEX_KEYWORDS = re.compile(
    r"\b(create|build|design|implement|refactor|analyze|analyse|compare|research|"
    r"plan|investigate|evaluate|test|debug|migrate|integrate|architect|"
    r"review|audit|generate|deploy|configure|set\s+up|write\s+a)\b",
    re.IGNORECASE,
)


def _is_complex(task: str) -> bool:
    """Return True if the task is long or mentions a complex action."""
    return len(task) > _COMPLEX_THRESHOLD or bool(_COMPLEX_KEYWORDS.search(task))


# ─── LLM call (injectable) ────────────────────────────────────────────────────

async def _default_call_llm(task: str, backend: str) -> list[str]:
    """Break a task into subtasks using the state-selected backend.

    Routing:
      * A non-local ``backend`` (e.g. ``"deepseek_v4_flash"``) is honoured — the
        call goes through ``execute._send_to_backend``, the same transport the
        rest of the graph uses for cloud backends.  This lets gateway-driven
        complex requests decompose on the requested backend instead of forcing
        the local router (which may have no usable local model on this host).
      * ``"local"`` / empty ``backend`` falls back to ``LocalModelRouter``
        discovery — the original default behaviour, unchanged.

    Returns a list of subtask strings.  Falls back to ``[task]`` on any error
    (including an unreachable backend) so a hiccup never breaks the turn.
    """
    prompt = (
        "Break the following task into 3–5 atomic, actionable subtasks. "
        "Return ONLY a numbered list, one item per line, no extra commentary.\n\n"
        f"Task: {task}"
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        if backend and backend != "local":
            # Honour the requested / state-selected backend. Late attribute
            # lookup on the execute module keeps the test-injection seam
            # (`execute._send_to_backend`) intact.
            import core.nodes.execute as _execute

            full = await _execute._send_to_backend(messages, backend)
        else:
            # Default path: discover and stream from a local backend.
            from core.backends.local_router import LocalModelRouter

            router = LocalModelRouter()
            backends = await router.discover()
            best = router.get_best_backend(backends)
            if not best:
                return [task]
            full = ""
            async for chunk in router.chat(messages, backend=best):
                full += chunk
    except Exception:
        logger.warning(
            "decompose backend call failed (backend=%r); "
            "falling back to single task",
            backend,
            exc_info=True,
        )
        return [task]

    lines = [
        re.sub(r"^\d+[.)]\s*", "", line.strip())
        for line in full.splitlines()
        if re.match(r"^\d+[.)]", line.strip())
    ]
    return lines if lines else [task]


# Module-level reference — replace in tests via monkeypatch
_call_llm: Callable[[str, str], Awaitable[list[str]]] = _default_call_llm


# ─── Node ─────────────────────────────────────────────────────────────────────

async def decompose_node(state: "AgentState") -> dict:
    """LangGraph node: decompose complex tasks into subtasks."""
    task = state.get("task", "")

    if not _is_complex(task):
        # Simple task — pass straight through with a bookkeeping message
        return {
            "messages": [
                {"role": "system", "content": f"Simple task (no decomposition needed): {task}"}
            ],
        }

    backend = state.get("backend", "local")
    subtasks = await _call_llm(task, backend)

    subtask_body = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(subtasks))
    return {
        "subtasks": subtasks,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"Task decomposed into {len(subtasks)} subtask(s):\n{subtask_body}"
                ),
            }
        ],
    }
