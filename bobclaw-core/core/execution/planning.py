"""Untrusted planner contracts and deterministic supplied-evidence checks.

The owner's acceptance contract supplies the checks. A planner can assign work,
but cannot weaken those checks, create a capability, or declare itself done.
"""
from __future__ import annotations

import json
import re

from .models import ExecutionError, integer, only, text

OUTPUT_KINDS = {"answer", "report", "proposal", "json", "intermediate"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def strict_json(raw):
    def reject_constant(value):
        raise ValueError("non-JSON number")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=pairs)


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ExecutionError("invalid_plan_id")
    return value


def validate_checks(checks):
    only(checks, {"min_length", "max_length", "required_sections", "require_citations", "json_type"})
    lo = integer(checks.get("min_length", 1), 1, 60000)
    integer(checks.get("max_length", 60000), lo, 60000)
    sections = checks.get("required_sections", [])
    if (not isinstance(sections, list) or len(sections) > 32
            or any(not isinstance(s, str) or not s.strip() or len(s) > 200 for s in sections)):
        raise ExecutionError("invalid_sections")
    if type(checks.get("require_citations", False)) is not bool:
        raise ExecutionError("invalid_acceptance")
    kind = checks.get("json_type", "object")
    if not isinstance(kind, str) or kind not in {"object", "array", "string", "number", "boolean", "null"}:
        raise ExecutionError("invalid_acceptance")


def acceptance_contract(value, worker_contract=None):
    if worker_contract is not None:
        from .checked import CONTRACT, acceptance
        if worker_contract != CONTRACT:
            raise ExecutionError('unsupported_worker_contract')
        return acceptance(value)
    only(value, {"criteria"})
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 100:
        raise ExecutionError("invalid_acceptance")
    seen = set()
    for criterion in criteria:
        only(criterion, {"id", "required", "output_kind", "checks"})
        key = identifier(criterion.get("id"))
        if key in seen or type(criterion.get("required", True)) is not bool:
            raise ExecutionError("invalid_acceptance")
        seen.add(key)
        if not isinstance(criterion.get("output_kind"), str) or criterion["output_kind"] not in OUTPUT_KINDS - {"intermediate"}:
            raise ExecutionError("unknown_output_kind")
        validate_checks(criterion.get("checks"))
    if not any(c.get("required", True) for c in criteria):
        raise ExecutionError("required_acceptance_missing")
    return value


def parse_plan(raw, contract, *, max_depth=4):
    try:
        value = strict_json(raw)
    except (ValueError, TypeError, RecursionError):
        raise ExecutionError("invalid_plan_schema") from None
    only(value, {"tasks"})
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 64:
        raise ExecutionError("invalid_plan_schema")
    criteria = {c["id"]: c for c in contract["criteria"]}
    by_id = {}
    for task in tasks:
        only(task, {"id", "instruction", "dependencies", "output_kind", "criteria_ids", "checks"})
        key = identifier(task.get("id"))
        if key in by_id:
            raise ExecutionError("duplicate_task")
        by_id[key] = task
        text(task.get("instruction"), 10000)
        if not isinstance(task.get("output_kind"), str) or task["output_kind"] not in OUTPUT_KINDS:
            raise ExecutionError("unknown_output_kind")
        validate_checks(task.get("checks", {}))
        for field in ("dependencies", "criteria_ids"):
            values = task.get(field)
            if not isinstance(values, list) or len(values) > 100:
                raise ExecutionError("invalid_plan_schema")
            for item in values:
                identifier(item)
            if len(set(values)) != len(values):
                raise ExecutionError("invalid_plan_schema")
        for cid in task["criteria_ids"]:
            if cid not in criteria or task["output_kind"] != criteria[cid]["output_kind"]:
                raise ExecutionError("invalid_criterion_assignment")
        if task["output_kind"] == "intermediate" and task["criteria_ids"]:
            raise ExecutionError("invalid_criterion_assignment")
    pending, done, depth = set(by_id), set(), 0
    while pending:
        if any(set(by_id[k]["dependencies"]) - set(by_id) for k in pending):
            raise ExecutionError("unknown_dependency")
        wave = {k for k in pending if set(by_id[k]["dependencies"]) <= done}
        if not wave:
            raise ExecutionError("plan_cycle")
        depth += 1
        if depth > max_depth:
            raise ExecutionError("plan_too_deep")
        if len(wave) > 16:
            raise ExecutionError("plan_too_wide")
        pending -= wave
        done |= wave
    # Layer width alone misses asynchronous waves: completing one source could
    # release sixteen children while fifteen other sources remain ready. The
    # maximum antichain (Dilworth matching) bounds every possible ready wave.
    ancestors = {}
    def above(key):
        if key not in ancestors:
            ancestors[key] = set(by_id[key]["dependencies"])
            for dependency in by_id[key]["dependencies"]:
                ancestors[key] |= above(dependency)
        return ancestors[key]
    edges = {key: {other for other in by_id if key in above(other)} for key in by_id}
    matched = {}
    def augment(key, visited):
        for other in edges[key] - visited:
            visited.add(other)
            if other not in matched or augment(matched[other], visited):
                matched[other] = key
                return True
        return False
    for key in by_id:
        augment(key, set())
    if len(by_id) - len(matched) > 16:
        raise ExecutionError("plan_too_wide")
    return value


def check_output(raw, task, contract, evidence_ids):
    """Check exact returned content; receipts are facts, never model opinions.

    Citations use structured IDs only. Inline citations use [source:<ID>] and
    are validated as well; arbitrary prose is not interpreted as evidence.
    """
    failures = []
    try:
        value = strict_json(raw)
        only(value, {"content", "citations"})
        content = text(value.get("content"))
        citations = value.get("citations")
        if not isinstance(citations, list) or any(not isinstance(c, str) for c in citations):
            raise ExecutionError("invalid_output_schema")
    except (ValueError, TypeError, RecursionError, ExecutionError):
        return {"passed": False, "failures": ["schema_valid"], "criteria_ids": []}
    inline = re.findall(r"\[source:([^\]]+)\]", content)
    if set(citations + inline) - set(evidence_ids):
        failures.append("supplied_evidence_only")
    criteria = {c["id"]: c for c in contract["criteria"]}
    checked = ["schema_valid", "non_empty", "supplied_evidence_only"]
    check_specs = [(cid, criteria[cid]["checks"]) for cid in task["criteria_ids"]]
    if task.get("checks"):
        check_specs.append(("task", task["checks"]))
    for cid, checks in check_specs:
        if not checks.get("min_length", 1) <= len(content) <= checks.get("max_length", 60000):
            failures.append(cid + ":length_bounds")
        if any(section not in content for section in checks.get("required_sections", [])):
            failures.append(cid + ":required_sections")
        if checks.get("require_citations") and not citations:
            failures.append(cid + ":citations_required")
        checked.extend([cid + ":length_bounds", cid + ":required_sections", cid + ":citations_required"])
    if task["output_kind"] == "json":
        try:
            parsed = strict_json(content)
            kind = ("null" if parsed is None else "boolean" if type(parsed) is bool else
                    "object" if isinstance(parsed, dict) else "array" if isinstance(parsed, list) else
                    "string" if isinstance(parsed, str) else "number")
            if any(checks.get("json_type", "object") != kind for _,checks in check_specs):
                failures.append("json_type")
        except (ValueError, RecursionError):
            failures.append("json_valid")
        checked.append("json_valid")
    return {"passed": not failures, "failures": failures, "checks": checked,
            "criteria_ids": task["criteria_ids"] if not failures else [],
            "content": content, "citations": citations}
