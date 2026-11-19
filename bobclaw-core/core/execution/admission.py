"""Shared resolver and admission. Catalog failure never imports a deployment roster."""
from __future__ import annotations

from .models import (CAPABILITIES, LOOP_TOPOLOGY, ExecutionError, definition, eligibility, integer,
                     only, request_key, start_request)


class Admission:
    def __init__(self, store, catalog):
        self.store, self.catalog = store, catalog
        self.enabled = True

    async def models(self):
        try:
            return await self.catalog()
        except Exception:
            return None

    async def teams(self, auth, args):
        only(args, {"cursor", "limit", "contract_version"})
        limit = integer(args.get("limit", 20), 1, 50)
        rows, cursor = await self.store.teams(auth, args.get("cursor"), limit)
        models = await self.models()
        from core.teams import execution_drafts
        try:
            drafts = execution_drafts(models or {})
        except Exception:
            drafts = []
        return {"items": [{**row, **eligibility(row, models)} for row in rows],
                "legacy_drafts": [{**row, **eligibility(row, models)} for row in drafts[:limit]],
                "legacy_drafts_truncated": len(drafts) > limit,
                "next_cursor": cursor, "capabilities_revision": CAPABILITIES["capabilities_revision"]}

    async def save(self, auth, args):
        only(args, {"contract_version", "definition", "expected_revision", "request_key"})
        request_key(args.get("request_key"))
        if "expected_revision" not in args:
            raise ExecutionError("expected_revision_required")
        expected = args["expected_revision"]
        if expected is not None:
            integer(expected)
        team = definition(args.get("definition"))
        result = await self.store.save_team(auth, team, expected, args["request_key"], args)
        computed = eligibility(result["team"], await self.models())
        return {**result, **computed, "team": {**result["team"], **computed}}

    async def start(self, auth, args):
        from .call_guard import required
        if required.get():
            raise ExecutionError("nested_run_start_forbidden", 403)
        request = start_request(args)
        if request.get('worker_contract'):
            raise ExecutionError('worker_contract_requires_jobs_start', 409)
        if not self.enabled:
            raise ExecutionError("execution_executor_unavailable", 503)
        replay = await self.store.replay(auth, "runs.start", request["request_key"], request)
        if replay:
            return {**await self.store.get(auth, replay["run_id"]), "operation_id": replay["operation_id"]}
        team = await self.store.team(auth, request["team"], request["team_revision"])
        if team["topology"] == LOOP_TOPOLOGY:
            from .planning import acceptance_contract
            acceptance_contract(request.get("acceptance"), request.get('worker_contract'))
        elif request["budget"]["start_calls"] < 4:
            raise ExecutionError("insufficient_call_allowance")
        models = await self.models()
        computed = eligibility(team, models)
        if not computed["runnable"]:
            raise ExecutionError("team_blocked", 409, computed)
        snapshot = {"team": team, "models": {key: models[key] for key in set(team["roles"].values()) | set(team.get("audit_bindings", [])) if key in models},
                    "review_independence": computed["review_independence"]}
        receipt = await self.store.start(auth, request, snapshot)
        return {**await self.store.get(auth, receipt["run_id"]), "operation_id": receipt["operation_id"]}

    async def revalidate(self, row):
        models = await self.models()
        computed = eligibility(row["snapshot"]["team"], models)
        if not computed["runnable"]:
            raise ExecutionError("capability_revoked", 409, computed)
        for key, pin in row["snapshot"]["models"].items():
            if key not in row["snapshot"]["team"]["roles"].values():
                continue  # audit health is intersected separately, at audit dispatch
            current = models.get(key)
            if not current or any(current.get(k) != pin.get(k) for k in ("backend", "model", "transport", "exact_calls")):
                raise ExecutionError("binding_changed", 409)

    async def prepare(self, auth, args):
        only(args, {"request", "request_key", "run_id", "expected_revision"})
        request_key(args.get("request_key"))
        replay = await self.store.replay(auth, "grants.prepare", args["request_key"], args)
        if replay:
            return replay
        if args.get("run_id"):
            if "request" in args:
                raise ExecutionError("conflicting_proposal")
            integer(args.get("expected_revision"))
            async with self.store.transaction() as conn:
                row = await self.store._row(conn, auth, args["run_id"])
            from .models import Authority
            original = Authority(**row["run_authority"])
            await self.revalidate(row)
            return await self.store.prepare_grant(auth, args, row["request"], row["snapshot"], original, row["id"])
        request = start_request(args.get("request"))
        if request.get("intent_id"):
            raise ExecutionError("nested_admission_intent")
        team = await self.store.team(auth, request["team"], request["team_revision"])
        if team["topology"] == LOOP_TOPOLOGY:
            from .planning import acceptance_contract
            acceptance_contract(request.get("acceptance"), request.get('worker_contract'))
        elif request["budget"]["start_calls"] < 4:
            raise ExecutionError("insufficient_call_allowance")
        models = await self.models()
        computed = eligibility(team, models)
        if not computed["runnable"]:
            raise ExecutionError("team_blocked", 409, computed)
        snapshot = {"team": team, "models": {key: models[key] for key in set(team["roles"].values()) | set(team.get("audit_bindings", [])) if key in models},
                    "review_independence": computed["review_independence"]}
        return await self.store.prepare_grant(auth, args, request, snapshot)
