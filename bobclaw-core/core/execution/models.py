"""Versioned execution contracts and deterministic validation (no IO)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

TOPOLOGY = "four-step-readonly/v1"
LOOP_TOPOLOGY = "agent-loop/v1"
ROLES = ("manager", "worker", "critic", "manager")
TERMINAL = {"completed", "cancelled", "failed"}
CAPABILITIES = {
    "contract_versions": [2], "topologies": [TOPOLOGY, LOOP_TOPOLOGY],
    "start_calls": [10, 50, 100, 200], "custom_calls": True,
    "confirmation_threshold": 200, "above_threshold_reason": "matching_owner_grant_required",
    "max_integer": "9223372036854775807", "null_wall": True,
    "integer_encoding": "safe_number_or_decimal_string", "grant_matcher_versions": [1, 2, 3],
    "auto_escalation": True, "resume": True, "worker_jobs": True,
    "job_time": {"version": 1, "presets_seconds": [900, 1800, 3600],
                 "agent_proposes": True, "owner_override": "reviewed_grant",
                 "strict_limit": True, "finite_custom": "matching_owner_grant",
                 "above_3600": "allow_long_runs_and_allow_extended_jobs",
                 "custom": "allow_custom_job_limit", "matcher_version": 3,
                 "continuation": "trusted_adapter_reconciliation_required",
                 "max_job_attempts_default": 32, "job_seconds_budget": True,
                 "cli_usage": "estimated_or_unknown", "codex_cli": False, "jobs_api": True},
    "growth": {"presets": [10, 50, 100, 200], "above_200": "double_with_owner_grant",
               "output_basis": "accepted_deliverable", "no_progress_cycles": 3,
               "topologies": [LOOP_TOPOLOGY]},
    "loop_contract": {"acceptance": "criteria_with_ids_output_kinds_and_deterministic_checks",
                      "max_depth": 4, "max_ready_wave": 16, "max_tasks_per_plan": 64,
                      "output_kinds": ["answer", "report", "proposal", "json", "intermediate"],
                      "receipt_page_size": 2},
    "project_pools": True, "sprint_pools": "storage_only_resource_service_unavailable",
    "owner_operations": {"runs.resume": "owner_stepup", "budgets.set": "owner_stepup"},
    "run_parallelism": 2, "follow_budget_seconds_per_call": 360,
    "failure_recovery": {"basic_retries": 2, "guided_retries": 1, "audit_binding_order": "team.audit_bindings",
                         "audit_requires_different_model": True, "retry_after_max_seconds": 60},
    "flags": {"list": True, "get": True, "resolve": "owner_stepup", "resolution_resumes": False},
    "server_run_list": True, "capabilities_revision": "execution/2.6",
    "worker_loop": {"contracts": ["checked-edits/1"], "topology": LOOP_TOPOLOGY,
                    "admission": "jobs.start_coder", "registered_operations_only": True,
                    "operation_ids": ["edits.plan","workspace.read","workspace.edit","python.scaffold",
                                      "files.nonempty","python.import","python.collect","python.pytest","python.cli"],
                    "max_depth": 16, "max_ready_wave": 16,
                    "step_kinds": ["model_task", "workspace_read", "workspace_edit", "worker_command",
                                   "registered_check", "proposal_publish"],
                    "apply": False, "cli": False},
}


class ExecutionError(Exception):
    def __init__(self, code, status=422, detail=None):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def only(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ExecutionError("unsupported_fields")


def integer(value, minimum=1, maximum=9223372036854775807):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExecutionError("invalid_integer")
    return value


INTEGER_FIELDS = frozenset({"start_calls", "wall_seconds", "max_job_attempts", "job_seconds_budget", "job_override_seconds", "team_revision", "expected_revision", "proposal_revision",
                            "call_limit", "call_ceiling", "job_seconds_ceiling", "limit", "receipt_cursor", "after_event_seq"})
SAFE_INTEGER = 9007199254740991


def decode_integers(value):
    """Negotiated HTTP/JSONL encoding; never accept already-rounded JS numbers."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in INTEGER_FIELDS and item is not None:
                if isinstance(item, str):
                    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", item):
                        raise ExecutionError("invalid_integer")
                    item = integer(int(item), 0)
                elif type(item) is not int or not 0 <= item <= SAFE_INTEGER:
                    raise ExecutionError("invalid_integer")
            result[key] = decode_integers(item)
        return result
    if isinstance(value, list):
        return [decode_integers(item) for item in value]
    return value


def wire_integers(value):
    if type(value) is int and abs(value) > SAFE_INTEGER:
        return str(value)
    if isinstance(value, dict):
        return {key: wire_integers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_integers(item) for item in value]
    return value


def text(value, maximum=60000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ExecutionError("invalid_text")
    return value


def slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ExecutionError("invalid_team_id")
    return value


def request_key(value):
    return text(value, 128)


@dataclass(frozen=True)
class Authority:
    lane: str
    owner: str
    actor: str
    scope: dict
    token_type: str
    credential_id: str = ""
    policy_epoch: str = ""
    credential_epoch: int | None = None
    session_kind: str = ""
    audience: str = ""
    allowed_operations: tuple = ()
    portal_credential_id: str = ""
    broker_link_nonce: str = ""

    @property
    def boundary(self):
        return digest({"actor": self.actor, "scope": self.scope, "token_type": self.token_type})

    def permits(self, row):
        return (row["lane"] == self.lane and row["owner"] == self.owner
                and (self.token_type != "agent" or row["boundary"] == self.boundary))

    def as_dict(self):
        from dataclasses import asdict
        return asdict(self)


def definition(value):
    only(value, {"schema_version", "id", "state", "topology", "roles", "review_independence",
                 "origin", "permitted_tools", "job_profiles", "max_parallelism", "audit_bindings"})
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ExecutionError("unsupported_team_schema")
    slug(value.get("id"))
    if "topology" in value:
        text(value["topology"], 100)
    if value.get("state", "saved") not in {"draft", "saved"}:
        raise ExecutionError("invalid_team_state")
    if not isinstance(value.get("roles", {}), dict) or set(value.get("roles", {})) - {"manager", "worker", "critic"}:
        raise ExecutionError("invalid_roles")
    for binding in value.get("roles", {}).values():
        if binding is not None:
            text(binding, 200)
    audits = value.get("audit_bindings", [])
    if (not isinstance(audits, list) or len(audits) > 16
            or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in audits)
            or len(set(audits)) != len(audits)):
        raise ExecutionError("invalid_audit_bindings")
    if value.get("review_independence", "same_model") not in {"same_model", "different_model"}:
        raise ExecutionError("invalid_independence")
    for field in ("permitted_tools", "job_profiles"):
        if (not isinstance(value.get(field, []), list) or len(value.get(field, [])) > 16
                or any(not isinstance(x, str) or len(x) > 128 for x in value.get(field, []))):
            raise ExecutionError("invalid_capabilities")
    if "origin" in value:
        origin = value["origin"]
        only(origin, {"source", "digest", "proposed_alternatives"})
        if origin.get("source") != "joat-yaml" or not re.fullmatch(r"[a-f0-9]{64}", str(origin.get("digest", ""))):
            raise ExecutionError("invalid_origin")
        alternatives = origin.get("proposed_alternatives", {})
        only(alternatives, {"manager", "worker", "critic"})
        if any(not isinstance(v, list) or len(v) > 16 or any(not isinstance(x, str) or len(x) > 128 for x in v)
               for v in alternatives.values()):
            raise ExecutionError("invalid_origin")
    integer(value.get("max_parallelism", 1), 1, 16)
    result = dict(value)
    result.setdefault("topology", TOPOLOGY)
    result.setdefault("state", "saved")
    result.setdefault("review_independence", "same_model")
    return result


def eligibility(team, models):
    reasons = []
    if models is None:
        reasons.append("catalog_unavailable")
    if team.get("state") == "draft":
        reasons.append("team_is_draft")
    if team.get("topology") not in {TOPOLOGY, LOOP_TOPOLOGY}:
        reasons.append("unsupported_topology")
    if team.get("permitted_tools") or team.get("job_profiles"):
        reasons.append("worker_boundary_unavailable")
    pins = []
    for role in set(ROLES):
        binding = team.get("roles", {}).get(role)
        spec = (models or {}).get(binding)
        if not binding:
            reasons.append("missing_role:" + role)
        elif not spec or not spec.get("enabled"):
            reasons.append("model_unavailable:" + role)
        elif not spec.get("exact_calls", False):
            reasons.append("opaque_call_accounting:" + role)
        else:
            pins.append(spec["model"])
    roles = team.get("roles", {})
    worker = (models or {}).get(roles.get("worker"), {}).get("model")
    critic = (models or {}).get(roles.get("critic"), {}).get("model")
    independent = bool(worker and critic and worker != critic)
    if team.get("review_independence") == "different_model" and not independent:
        reasons.append("independent_review_required")
    return {"eligibility": "blocked" if reasons else "runnable", "runnable": not reasons,
            "blocked_reasons": sorted(set(reasons)),
            "review_independence": "different_model" if independent else "same_model"}


def budget(value, *, minimum=4, check_grant=True):
    only(value, {"start_calls", "wall_mode", "wall_seconds", "max_job_attempts", "job_seconds_budget"})
    calls = integer(value.get("start_calls"))
    if calls < minimum:
        raise ExecutionError("insufficient_call_allowance")
    if calls > 200 and check_grant:
        raise ExecutionError("confirmation_required", 409,
                             {"blocked_reasons": ["owner_grant_service_unavailable"]})
    mode, seconds = value.get("wall_mode"), value.get("wall_seconds")
    if mode not in {"fixed", "follow_budget", "none"}:
        raise ExecutionError("invalid_wall_mode")
    if mode == "none":
        if seconds is not None:
            raise ExecutionError("conflicting_wall_fields")
    else:
        # Reject timestamp overflow before admission; no product wall-time cap.
        remaining = int((datetime.max.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()) - 60
        integer(seconds, 1, remaining)
    result = {"start_calls": calls, "wall_mode": mode, "wall_seconds": seconds}
    if "max_job_attempts" in value:
        result["max_job_attempts"] = integer(value["max_job_attempts"])
    if "job_seconds_budget" in value:
        result["job_seconds_budget"] = integer(value["job_seconds_budget"]) if value["job_seconds_budget"] is not None else None
    return result


def start_request(value):
    only(value, {"contract_version", "team", "team_revision", "task", "request_key", "budget",
                 "project_id", "conversation_id", "sprint_id", "occurrence_id", "evidence_ids",
                 "acceptance", "intent_id", "job_coverage", "worker_contract", "work_mode", "work_recipe"})
    if type(value.get("contract_version")) is not int or value["contract_version"] != 2:
        raise ExecutionError("unsupported_run_contract")
    slug(value.get("team"))
    integer(value.get("team_revision"))
    text(value.get("task"))
    request_key(value.get("request_key"))
    if any(value.get(k) is not None for k in ("sprint_id", "occurrence_id")):
        raise ExecutionError("resource_service_unavailable", 409)
    for field in ("project_id", "conversation_id"):
        if value.get(field) is not None:
            text(value[field], 128)
    ids = value.get("evidence_ids", [])
    if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(x, str) or len(x) > 512 for x in ids):
        raise ExecutionError("invalid_evidence_ids")
    if "acceptance" in value:
        if isinstance(value["acceptance"], dict):
            from .planning import acceptance_contract
            acceptance_contract(value["acceptance"], value.get('worker_contract'))
            if len(canonical(value["acceptance"])) > 30000:
                raise ExecutionError("invalid_acceptance")
        else:
            text(value["acceptance"], 10000)
    result = dict(value)
    if 'work_mode' in value and (value['work_mode'] not in {'builder','fixer'} or value.get('worker_contract') != 'checked-edits/1'):
        raise ExecutionError('invalid_work_mode')
    if 'work_recipe' in value:
        from .checked import pinned_recipe
        pinned_recipe(value['work_recipe'])
        if 'work_mode' not in value:raise ExecutionError('invalid_work_mode')
    elif 'work_mode' in value:
        raise ExecutionError('recipe_pin_required', 409)
    if 'worker_contract' in value:
        from .checked import CONTRACT
        if value['worker_contract'] != CONTRACT or not isinstance(value.get('acceptance'), dict):
            raise ExecutionError('unsupported_worker_contract')
    result["budget"] = budget(value.get("budget"), minimum=1, check_grant=False)
    if "job_coverage" in value:
        from .job_policy import coverage
        scope = coverage(value["job_coverage"])
        if scope["allow_cli"] or "codex" in scope["profiles"]:
            raise ExecutionError("cli_jobs_unavailable", 409)
        if result["budget"].get("max_job_attempts", 32) > scope["max_job_attempts"]:
            raise ExecutionError("job_coverage_budget_conflict")
        if scope.get("job_seconds_budget") is not None and (result["budget"].get("job_seconds_budget") is None or result["budget"]["job_seconds_budget"] > scope["job_seconds_budget"]):
            raise ExecutionError("job_coverage_budget_conflict")
    return result
