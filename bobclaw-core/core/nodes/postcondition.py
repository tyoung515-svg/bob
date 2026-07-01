"""BoBClaw Core — verification-spine §2.6 tier-1 graph node.

A reusable LangGraph node that verifies a declared **post-condition** with a decorrelated
(cross-family) critic. Both lanes (research §3 / GUI §4) and MS-3 call it. The node reads
``state["post_condition"]`` (the declared spec) and writes ONLY ``post_condition_verdict``
(a plain dict) — so it composes into any subgraph. It NEVER raises out: every failure
surfaces as a not-passing verdict (the tier-1 fail-safe posture).
"""
from __future__ import annotations

from typing import Any

from core.verify.postcondition import PostConditionError, verify_post_condition


async def postcondition_node(state: dict[str, Any]) -> dict:
    """Verify ``state["post_condition"]`` with a decorrelated critic.

    ``post_condition``: ``{step, statement (or "post_condition"), result, actor_backend?,
    critic_backend?}``. ``actor_backend`` falls back to ``state["backend"]`` then ``"local"``;
    the team is ``state["team"]``. No spec / no statement → an ``unknown`` not-passed verdict.
    """
    raw_pc = state.get("post_condition")
    pc = raw_pc if isinstance(raw_pc, dict) else {}
    actor = pc.get("actor_backend") or state.get("backend") or "local"
    team = state.get("team")
    critic_backend = pc.get("critic_backend")
    statement = pc.get("statement") or pc.get("post_condition") or ""

    if not statement:
        return {
            "post_condition_verdict": {
                "verdict": "unknown",
                "passed": False,
                "reasons": ["no post-condition declared"],
                "actor_backend": actor,
                "critic_backend": critic_backend,
                "decorrelated": False,
            }
        }

    try:
        res = await verify_post_condition(
            step=str(pc.get("step", "")),
            statement=str(statement),
            result=str(pc.get("result", "")),
            actor_backend=actor,
            team=team,
            critic_backend=critic_backend,
        )
    except PostConditionError as exc:
        return {
            "post_condition_verdict": {
                "verdict": "unknown",
                "passed": False,
                "reasons": [f"postcondition_error: {exc}"],
                "actor_backend": actor,
                "critic_backend": critic_backend,
                "decorrelated": False,
            }
        }
    except Exception as exc:  # noqa: BLE001 — belt: the node NEVER raises out (tier-1 fail-safe)
        # NOTE: deliberately Exception, NOT BaseException — asyncio.CancelledError (BaseException
        # in py3.8+) MUST propagate so cooperative cancellation/shutdown still works; swallowing it
        # would wedge cancellation. A cancelled turn is not a "verdict".
        return {
            "post_condition_verdict": {
                "verdict": "unknown",
                "passed": False,
                "reasons": [f"postcondition_node_error: {type(exc).__name__}: {exc}"],
                "actor_backend": actor,
                "critic_backend": critic_backend,
                "decorrelated": False,
            }
        }
    return {"post_condition_verdict": res.as_dict()}
