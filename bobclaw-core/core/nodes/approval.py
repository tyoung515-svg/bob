"""
BoBClaw Core — Human-in-the-loop approval gate

Pause-before-node model:

* The graph is compiled with ``interrupt_before=["approval"]`` in
  :func:`core.graph.build_graph`, so the graph self-pauses at the
  checkpoint boundary before this node ever runs.
* The HTTP layer (``/api/chat/approval``) records the user's decision
  by calling ``graph.aupdate_state(config, {"approval_response": ...})``
  and then resumes the turn with ``graph.astream(None, config)``.
* On resume, this node reads ``state.approval_response`` and either
  emits a rejection message or clears the approval flags so execute
  can proceed.

Why not ``langgraph.types.interrupt``?  That primitive calls
``get_config()`` which reads a ContextVar that is not reliably
propagated on Python 3.10 (LangGraph's ``ASYNCIO_ACCEPTS_CONTEXT``
guard only trips on 3.11+).  ``interrupt_before`` sidesteps the
ContextVar path entirely, at the cost of losing the "passthrough
payload" feature of ``interrupt()`` — which we don't use anyway.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.graph import AgentState


def approval_node(state: "AgentState") -> dict:
    """LangGraph node: translate ``state.approval_response`` into flags.

    Invariants (enforced by ``_route_from_execute`` + ``interrupt_before``):
        * This node only runs after ``execute_node`` raised ``approval_required``
          and ``/api/chat/approval`` populated ``approval_response``.
        * If ``approval_required`` is False the node is a no-op (belt-and-braces
          for tests that invoke it directly).

    Decision handling:
        reject / rejected → appends a system rejection message, normalises
                             ``approval_response`` to ``"rejected"``, clears
                             ``approval_required``.  ``execute_node`` detects
                             the string and terminates the turn.
        any other value    → clears ``approval_required``; ``execute_node``
                             skips its first-pass approval check and calls
                             the backend.
        None               → shouldn't occur under ``interrupt_before``; we
                             fall through as a no-op to avoid an infinite
                             loop if misconfigured.
    """
    if not state.get("approval_required"):
        return {}

    raw = state.get("approval_response")
    if raw is None:
        return {}

    decision = str(raw).strip().lower()

    if decision in {"reject", "rejected"}:
        return {
            "messages": [
                {"role": "system", "content": "Action rejected by user."}
            ],
            "approval_required": False,
            "approval_response": "rejected",
        }

    # Approved — clear flag so execute_node proceeds past its approval check.
    return {
        "approval_required": False,
        "approval_response": decision,
    }
