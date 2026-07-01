"""
BoBClaw Core — Gate Router decision node (Phase 2).

``gate_decide`` evaluates an action against a job's declared ``scope:`` block.
Deterministic checks run first; ambiguous actions are sent to the critic
(the senior/auditor tier) for reconciliation against the scope. Fail closed:
any error or uncertainty routes to a human.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.config import config
from core.nodes.critic import run_critic
from core.permissions import Scope, evaluate_action, evaluate_path


@dataclass
class GateDecision:
    """Result of a Gate Router evaluation."""

    destination: str  # one of "auto" | "gate" | "human"
    reasons: list[str] = field(default_factory=list)
    critic_verdict: Optional[str] = None


# Priority order: most restrictive destination wins.
_DESTINATION_PRIORITY = {"auto": 0, "gate": 1, "human": 2}


def _combine_destinations(*destinations: str) -> str:
    """Return the most restrictive destination."""
    return max(destinations, key=lambda d: _DESTINATION_PRIORITY.get(d, -1))


GATE_RECONCILE_PROMPT: str = """You are the Gate Router's reconciliation critic.

A worker has proposed an action. You must decide whether it reconciles to the
job's pre-approved scope (the blast radius the operator already signed off on).

Job scope (the pre-approved blast radius):
```json
{subtask_text}
```

Proposed action:
{worker_output}

Evaluate whether the proposed action is clearly within the scope's intent and
safe to auto-clear. Consider:
- Is the action type in ``auto_actions`` or naturally implied by it?
- Are any touched paths covered by ``may_touch`` and not blocked by ``may_not_touch``?
- Does the action respect ``branch`` and ``budget_usd``?

Respond with a JSON object on a single line:
{{"verdict": "approve" | "flag" | "reject", "reasons": ["short reason 1"]}}

- "approve": clearly within scope and safe to auto-clear.
- "flag": ambiguous, partially out of scope, or worth human review.
- "reject": clearly out of scope, unsafe, or inconsistent with the scope.

Respond with the JSON object and nothing else."""


WORKER_SCOPE_REVIEW_PROMPT: str = """You are reviewing a fan-out worker's output for scope drift.

{subtask_text}

Worker output:
{worker_output}

Evaluate whether the worker's output stays within the job's declared blast radius.
Does it propose or assume work that is outside the scope? Does it touch paths in
``may_not_touch``, perform actions in ``escalate_actions``, or exceed the scope's
intent or budget?

Respond with a JSON object on a single line:
{{"verdict": "approve" | "flag" | "reject", "reasons": ["short reason 1"]}}

- "approve": output stays within the scope's intent and blast radius.
- "flag": minor scope drift or ambiguity worth surfacing for review.
- "reject": clearly proposes or assumes out-of-scope work.

Respond with the JSON object and nothing else."""


async def gate_decide(
    action_type: str,
    details: dict,
    scope: Optional[Scope],
    critic_backend: Optional[str] = None,
) -> GateDecision:
    """Evaluate an action against a job scope and return its Gate destination.

    Args:
        action_type: The action being evaluated (e.g. ``"cc_edit"``, ``"read"``).
        details: Action-specific details. May include ``file_paths`` for path checks.
        scope: The job's parsed ``Scope`` block, or ``None``.
        critic_backend: Optional backend to run the critic on ambiguous actions.
            Falls back to ``config.GATE_CRITIC_BACKEND`` when not provided.

    Returns:
        A ``GateDecision`` with destination ``"auto"``, ``"gate"``, or ``"human"``.
        ``"gate"`` is only returned when no critic backend is configured; otherwise
        the critic resolves it to ``"auto"`` or ``"human"``.
    """
    reasons: list[str] = []

    if not action_type:
        return GateDecision("human", reasons=["missing action_type"])

    if scope is None:
        return GateDecision("human", reasons=["missing scope"])

    # Action-level policy.
    action_dest = evaluate_action(action_type, scope)
    reasons.append(f"action {action_type!r} -> {action_dest}")

    # Path-level policy for any file_paths in details.
    file_paths = details.get("file_paths") or []
    if details.get("file_path"):
        file_paths = [details["file_path"], *file_paths]

    path_dest = "auto"
    for path in file_paths:
        dest = evaluate_path(path, scope)
        reasons.append(f"path {path!r} -> {dest}")
        path_dest = _combine_destinations(path_dest, dest)
        if path_dest == "human":
            break

    combined = _combine_destinations(action_dest, path_dest)

    effective_critic_backend = (
        critic_backend if critic_backend is not None else config.GATE_CRITIC_BACKEND
    )
    if combined == "gate" and effective_critic_backend:
        scope_json = scope.model_dump_json(indent=2)
        worker_output = f"action_type={action_type}\ndetails={details}"
        verdict, critic_reasons = await run_critic(
            subtask_text=scope_json,
            worker_output=worker_output,
            critic_backend=effective_critic_backend,
            prompt_template=GATE_RECONCILE_PROMPT,
        )
        if verdict == "approve":
            combined = "auto"
            reasons.append(f"critic approved: {'; '.join(critic_reasons)}")
        else:
            # flag, reject, critic unavailable
            combined = "human"
            reasons.append(
                f"critic {verdict}: {'; '.join(critic_reasons)}"
            )
        return GateDecision(
            destination=combined,
            reasons=reasons,
            critic_verdict=verdict,
        )

    return GateDecision(destination=combined, reasons=reasons)
