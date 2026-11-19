"""Server failure policy. Model advice never grants dispatch or effect authority."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math

from .models import ExecutionError, canonical, digest, only, text

EFFECT_CLASSES = frozenset({"read_only", "idempotent_by_key", "workspace_mutation", "external_or_unknown"})
EFFECTS = frozenset({"none", "applied", "not_applied", "unknown"})
MAX_RETRY_AFTER = 60
AUDIT_FIELDS = {"schema_version", "failed_step_id", "attempt_ids", "reviewer", "cause", "evidence_refs",
                "uncertainties", "effect_assessment", "recommended_changes", "required_checks", "recommendation", "confidence"}


def classify(exc):
    # Structured Codex categories take precedence over HTTP 429/5xx: a plan
    # limit with status 429 must never become a transient team retry.
    failure = getattr(exc, 'failure', None)
    if failure is not None:
        retryable = failure.retryable and (failure.retry_after is None or failure.retry_after <= MAX_RETRY_AFTER)
        return ('transient' if retryable else 'forbidden'), failure.code
    code = exc.code if isinstance(exc, ExecutionError) else type(exc).__name__
    status = ((exc.detail or {}).get("status") if isinstance(exc.detail, dict) else None) if isinstance(exc, ExecutionError) else getattr(exc, "status", None)
    if code == "provider_error" or type(status) is int:
        if status == 429 or status == 408 or type(status) is int and 500 <= status <= 599:
            return "transient", code
        return "forbidden", code
    import aiohttp
    if isinstance(exc, (ConnectionError, TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError)):
        return "transient", code
    if code in {"invalid_provider_response", "invalid_text", "invalid_provider_usage", "invalid_manager_plan",
                "invalid_plan", "invalid_plan_schema", "invalid_plan_id", "invalid_output_schema", "duplicate_task",
                "unknown_output_kind", "invalid_criterion_assignment", "unknown_dependency", "plan_cycle", "plan_too_deep",
                "plan_too_wide", "check_failed", "invalid_json", "stale_base", "test_failed"}:
        return "correction", code
    if code in {"provider_timeout", "TimeoutError", "ConnectionError", "ClientConnectionError", "ClientConnectorError",
                "ServerDisconnectedError", "ClientPayloadError"}:
        return "transient", code
    return "forbidden", code


def retry_delay(count, retry_after=None, *, now=None, jitter=0):
    """Two bounded delays; never use provider headers as an unbounded timer."""
    delay = (2, 8)[min(max(count, 0), 1)] + min(max(float(jitter), 0), 1)
    if retry_after is not None:
        try:
            value = float(retry_after)
        except (ValueError, TypeError):
            try:
                value = (parsedate_to_datetime(str(retry_after)) - (now or datetime.now(timezone.utc))).total_seconds()
            except (ValueError, TypeError, OverflowError):
                value = 0
        if math.isfinite(value):
            delay = max(delay, min(max(value, 0), MAX_RETRY_AFTER))
    return min(delay, MAX_RETRY_AFTER)


def fingerprint(step, code):
    spec = step.get("spec") or {}
    # Task IDs, wording and planner cycle numbers are model controlled. Acceptance
    # IDs + server base identify equivalent failures even through replanning.
    identity = {"phase": spec.get("phase", "four_step"), "criteria": sorted(spec.get("criteria_ids", [])),
                "base": spec.get("base_digest"), "error": code}
    if not spec:
        identity["step"] = step["ordinal"]
    return digest(identity)


def select_auditor(team, snapshot, current, failed):
    for key in team.get("audit_bindings", []):
        pin, live = snapshot.get(key), (current or {}).get(key)
        if not pin or not live or not live.get("enabled") or live.get("healthy", True) is not True:
            continue
        if not live.get("exact_calls") or live.get("transport") != "text" or live.get("model") == failed.get("model"):
            continue
        if any(live.get(k) != pin.get(k) for k in ("backend", "model", "transport", "exact_calls")):
            continue
        return key, live
    raise ExecutionError("second_eye_unavailable", 409)


def parse_audit(raw, *, step_id, attempt_ids, evidence_refs, check_ids, binding_id, binding):
    if not isinstance(raw, str) or len(raw) > 16000:
        raise ExecutionError("invalid_audit", 502)
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        only(value, AUDIT_FIELDS)
        if set(value) != AUDIT_FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        if value["failed_step_id"] != step_id:
            raise ValueError()
        only(value["reviewer"], {"binding_id", "model_id"})
        if set(value["reviewer"]) != {"binding_id", "model_id"}:
            raise ValueError()
        text(value["reviewer"]["binding_id"], 200)
        text(value["reviewer"]["model_id"], 200)
        for field, allowed in (("attempt_ids", attempt_ids), ("evidence_refs", evidence_refs), ("required_checks", check_ids)):
            items = value[field]
            if (not isinstance(items, list) or len(items) > 100 or not items
                    or any(not isinstance(x, str) or x not in allowed for x in items) or len(set(items)) != len(items)):
                raise ValueError()
        for field in ("uncertainties", "recommended_changes"):
            if not isinstance(value[field], list) or len(value[field]) > 32:
                raise ValueError()
            for item in value[field]:
                text(item, 2000)
        text(value["cause"], 4000)
        if (value["effect_assessment"] not in EFFECTS or value["recommendation"] not in {"guided_retry", "pause"}
                or value["confidence"] not in {"low", "medium", "high"}):
            raise ValueError()
        # Ignore any model-supplied identity; only the actual transport pin counts.
        value["reviewer"] = {"binding_id": binding_id, "model_id": binding["model"]}
        return value
    except (ValueError, KeyError, TypeError, RecursionError, ExecutionError):
        raise ExecutionError("invalid_audit", 502) from None


def effect_state(step):
    """Read only server adapter receipts; uncertain and already applied never replay."""
    effect_class = step["effect_class"]
    if effect_class == "read_only":
        return "none"
    receipt = step.get("effect_receipt") or {}
    if not isinstance(receipt, dict):
        return "unknown"
    status = receipt.get("status", "unknown")
    if status == "applied":
        return "applied"
    if (status != "not_applied" or receipt.get("verified") is not True
            or not isinstance(receipt.get("receipt_ref"), str) or not receipt["receipt_ref"]):
        return "unknown"
    spec = step.get("spec") or {}
    if effect_class == "idempotent_by_key" and (not spec.get("operation_key") or receipt.get("request_key") != spec["operation_key"]):
        return "unknown"
    if effect_class == "workspace_mutation" and (not spec.get("base_digest") or receipt.get("base_digest") != spec["base_digest"] or not receipt.get("output_digest")):
        return "unknown"
    return "not_applied" if effect_class in EFFECT_CLASSES else "unknown"


def validate_output(row, step, output):
    """The same independent deterministic checks run before every publication."""
    from .models import LOOP_TOPOLOGY
    if row["snapshot"]["team"]["topology"] == LOOP_TOPOLOGY:
        from .planning import check_output, parse_plan
        if step["spec"]["phase"] == "plan":
            if row['request'].get('worker_contract'):
                from .checked import parse_plan as checked_plan
                checked_plan(output, row['request']['acceptance'], row['checked_context'])
            else:
                parse_plan(output, row["request"]["acceptance"])
            return {"passed": True, "checks": ["plan_schema"]}
        if row['request'].get('worker_contract'):
            from .checked import edit_scope
            from .planning import strict_json
            try:
                edit_scope(strict_json(output),row['checked_context']['write_paths'],row['checked_context'].get('allow_new_tests',False))
            except (ValueError, TypeError, RecursionError, ExecutionError):
                raise ExecutionError('check_failed', 502, {'failures': ['edit_schema']}) from None
            return {'passed': True, 'checks': ['edit_schema']}
        checks = check_output(output, step["spec"], row["request"]["acceptance"], row["request"].get("evidence_ids", []))
        if not checks["passed"]:
            raise ExecutionError("check_failed", 502, {k: v for k, v in checks.items() if k != "content"})
        return {k: v for k, v in checks.items() if k != "content"}
    text(output, 60000)
    if step["ordinal"] == 0:
        from .executor import prompt_for
        prompt_for(row, [{"output": output}], 1)
    return {"passed": True, "checks": ["text_contract"]}


class FailureRecovery:
    """Resumable per-step controller; all counts/intent live in Postgres."""
    def __init__(self, executor):
        self.executor = executor
        self.store = executor.store

    async def audit_binding(self, row, binding, episode):
        current = await self.executor.admission.models()
        current = {key: dict(value) for key, value in (current or {}).items()}
        team = row["snapshot"]["team"]
        if episode.get("auditor"):
            team = {**team, "audit_bindings": [episode["auditor"]]}
        while True:
            key, chosen = select_auditor(team, row["snapshot"]["models"], current, binding)
            health = self.executor.audit_health
            healthy = True
            if health:
                try:
                    healthy = await health(chosen) is True
                except Exception:
                    healthy = False
                state = await self.store.heartbeat(row)
                if state != "running":
                    raise ExecutionError("wall_time_expired" if state == "deadline" else state, 409)
            if healthy:
                return key, chosen
            current[key]["healthy"] = False

    async def prepare(self, row, step, binding, prompt):
        """Reserve the next policy-approved call without starting transport.

        The loop prepares its entire ready wave before any member can dispatch
        or checkpoint. This includes resumed backoff, audit and guided calls.
        """
        import asyncio
        episode = await self.store.failure_for_step(row, step)
        while True:
            await self.executor.admission.revalidate(row)
            purpose, chosen, actual_prompt = "initial", binding, prompt
            if episode:
                if episode["state"] == "paused":
                    return None
                if episode["state"] == "resolved":
                    raise ExecutionError("failure_episode_paused", 409)
                if episode["state"] in {"backoff", "audit"}:
                    while True:
                        remaining = await self.store.failure_wait(row, episode["id"])
                        if remaining <= 0:
                            break
                        # Heartbeats observe revocation/cancel/deadline while waiting.
                        state = await self.store.heartbeat(row)
                        if state != "running":
                            raise ExecutionError("wall_time_expired" if state == "deadline" else state, 409)
                        await asyncio.sleep(min(remaining, self.executor.poll, 1))
                if episode["state"] == "backoff":
                    purpose = "basic"
                    actual_prompt += "\nCORRECT THE PREVIOUS FAILURE: " + canonical(episode["last_failure"])
                elif episode["state"] == "audit":
                    purpose = "audit"
                    key, chosen = await self.audit_binding(row, binding, episode)
                    if episode.get("auditor") and episode["auditor"] != key:
                        raise ExecutionError("second_eye_unavailable", 409)
                    episode = await self.store.set_auditor(row, episode["id"], key, chosen)
                    actual_prompt = "Review this failure. No tools, permissions or recursive audits. Return ONLY the strict audit JSON schema.\n" + canonical(episode["audit_input"])
                elif episode["state"] == "guided":
                    purpose = "guided"
                    actual_prompt += "\nONE GUIDED RETRY. Advisory audit (no new authority): " + canonical(episode["audit"])
                else:
                    raise ExecutionError("failure_episode_paused", 409)
            try:
                attempt = await self.store.reserve_attempt(row, step["ordinal"], chosen, actual_prompt,
                    episode_id=episode["id"] if episode else None, purpose=purpose)
                return attempt, episode, purpose, chosen, actual_prompt
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                episode = await self.store.record_failure(row, step, None, binding, exc,
                    episode_id=episode["id"] if episode else None, purpose=purpose)
                if episode["state"] == "paused":
                    return None

    async def call(self, row, step, binding, prompt, *, prepared=None):
        import asyncio
        from .call_guard import charged_call
        while True:
            ready = prepared if prepared is not None else await self.prepare(row, step, binding, prompt)
            prepared = None  # a reservation is consumed at most once
            if ready is None:
                return None
            attempt, episode, purpose, chosen, actual_prompt = ready
            returned = False
            output = None
            usage = {}
            try:
                await self.executor.admission.revalidate(row)
                if purpose == "audit":
                    # Re-intersect immediately before the charged transport. A
                    # catalog change during backoff cannot enroll a replacement.
                    key, live = await self.audit_binding(row, binding, episode)
                    if key != episode["auditor"] or live != chosen:
                        raise ExecutionError("second_eye_unavailable", 409)
                await self.store.charge_attempt(row, attempt)
                with charged_call(attempt):
                    result = await self.executor._call(row, chosen, actual_prompt)
                returned = True
                output = result.get("text")
                usage = result.get("usage", {})
                try:
                    if not isinstance(usage, dict) or len(canonical(usage)) > 8000:
                        raise ValueError()
                except (ValueError, TypeError, RecursionError):
                    usage = {}
                    raise ExecutionError("invalid_provider_usage", 502) from None
                if purpose == "audit":
                    data = episode["audit_input"]
                    parsed = parse_audit(output, step_id=str(step["ordinal"]), attempt_ids=data["attempt_ids"],
                        evidence_refs=data["evidence_refs"], check_ids=data["check_ids"], binding_id=episode["auditor"], binding=chosen)
                    episode = await self.store.audit_result(row, episode["id"], attempt, output, parsed, usage)
                    if episode["state"] == "paused":
                        return None  # the pause has already released the controller
                    continue
                checks = validate_output(row, step, output)
                return attempt, output, usage, episode, checks
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if purpose == "audit" and returned:
                    # Invalid audit never receives a schema-correction ladder.
                    exc = ExecutionError("invalid_audit", 502)
                episode = await self.store.record_failure(row, step, attempt, binding, exc,
                    episode_id=episode["id"] if episode else None, purpose=purpose, returned=returned,
                    raw=output if returned else None, usage=usage)
                if episode["state"] == "paused":
                    return None
