"""Server-owned admission intents and versioned owner grants (no inference)."""
from datetime import datetime
import json
import uuid

from .budgets import task_digest
from .models import Authority, ExecutionError, canonical, digest, integer, only, request_key, text


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def grant_matches(grant, row):
    if grant.get('matcher_version') == 4:
        coverage = decoded(grant.get('workflow_coverage'))
        snapshot = row.get('snapshot', {})
        return bool(row.get('topology') == 'workflow/v1' and coverage
                    and grant.get('run_id') == row.get('id')
                    and grant.get('authority_boundary') == row.get('boundary')
                    and grant.get('credential_epoch') == row.get('run_authority', {}).get('credential_epoch')
                    and grant.get('coverage_digest') == digest(coverage) == snapshot.get('coverage_digest')
                    and coverage == snapshot.get('workflow_coverage')
                    and coverage.get('resolved_digest') == digest(snapshot.get('resolved'))
                    and grant.get('team_digest') == digest(snapshot.get('team'))
                    and grant.get('models_digest') == digest(snapshot.get('models'))
                    and grant.get('call_ceiling') == coverage['budget']['calls'])
    if grant.get("credential_epoch") is not None and grant["credential_epoch"] != row.get("run_authority", {}).get("credential_epoch"):
        return False
    if (grant["authority_boundary"] != row["boundary"]
            or grant["team_digest"] != digest(row["snapshot"]["team"])
            or grant["models_digest"] != digest(row["snapshot"]["models"])):
        return False
    # v1 project records remain exact-task grants. Only an explicitly reviewed
    # v2 project grant approves future task text/evidence within the same boundary.
    if grant.get("matcher_version", 1) not in {1, 2, 3}:
        return False
    if grant.get("matcher_version", 1) == 3:
        from .job_policy import within_coverage
        if not within_coverage(decoded(grant.get("job_coverage")), row["request"].get("job_coverage")):
            return False
    if grant.get("matcher_version", 1) in {2, 3} and grant.get("coverage_kind") == "project":
        return bool(grant["project_id"] and grant["project_id"] == row["request"].get("project_id"))
    return (grant["task_digest"] == task_digest(row["request"])
            and (grant["run_id"] == row["id"] or
                 (grant.get("matcher_version", 1) == 1 and grant["run_id"] is None
                  and grant["project_id"] == row["request"].get("project_id"))))


def public_grant(row):
    result = dict(row)
    if "job_coverage" in result:
        result["job_coverage"] = decoded(result["job_coverage"])
    if isinstance(result.get("expires_at"), datetime):
        result["expires_at"] = result["expires_at"].isoformat()
    return result


class Grants:
    async def prepare_grant(self, auth, args, request, snapshot, original=None, run_id=None):
        """Admission has already resolved the exact team/models without dispatch."""
        request_key(args.get("request_key"))
        original = original or auth
        async with self.transaction() as conn:
            previous = await self._mutation(conn, auth, "grants.prepare", args["request_key"], args)
            if previous:
                return previous["result"]
            await self._team(conn, original, request["team"], request["team_revision"])
            if run_id:
                row = await self._row(conn, auth, run_id, lock=True)
                if (row["revision"] != args.get("expected_revision") or row["snapshot"] != snapshot
                        or task_digest(row["request"]) != task_digest(request)):
                    raise ExecutionError("revision_conflict", 409)
            proposal_id, planned_id = str(uuid.uuid4()), run_id or str(uuid.uuid4())
            # Normalize through JSON so the first reply equals a stored-receipt replay.
            coverage = json.loads(canonical({"initiator": original.as_dict(), "request": request, "snapshot": snapshot,
                                             "planned_run_id": planned_id, "matcher_version": 3 if request.get("job_coverage") else 2}))
            proposal_digest = digest(coverage)
            await conn.execute("INSERT INTO agent_admission_intents(lane,owner,id,run_id,boundary,authority,request,snapshot,proposal_digest,existing_run) "
                               "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10)",
                               auth.lane, auth.owner, proposal_id, planned_id, original.boundary,
                               canonical(original.as_dict()), canonical(request), canonical(snapshot), proposal_digest, bool(run_id))
            return await self._remember(conn, auth, "grants.prepare", args["request_key"], args, proposal_id,
                                        {"proposal_id": proposal_id, "intent_id": proposal_id, "revision": 1,
                                         "digest": proposal_digest, "coverage": coverage})

    async def _proposal(self, conn, auth, proposal_id):
        row = await conn.fetchrow("SELECT * FROM agent_admission_intents WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE",
                                  auth.lane, auth.owner, text(proposal_id, 128))
        if not row or not auth.permits(row):
            raise ExecutionError("not_found", 404)
        result = dict(row)
        for key in ("authority", "request", "snapshot"):
            result[key] = decoded(result[key])
        return result

    async def grant_proposal(self, auth, proposal_id):
        async with self.transaction() as conn:
            row = await self._proposal(conn, auth, proposal_id)
            return {"proposal_id": row["id"], "revision": row["revision"], "digest": row["proposal_digest"],
                    "coverage": {"initiator": row["authority"], "request": row["request"], "snapshot": row["snapshot"],
                                 "planned_run_id": row["run_id"], "matcher_version": 3 if row["request"].get("job_coverage") else 2}}

    async def issue_grant(self, auth, args):
        only(args, {"proposal_id", "proposal_revision", "expected_revision", "request_key", "coverage_kind", "allow_long_runs",
                    "auto_escalate", "call_ceiling", "expires_at", "allow_extended_jobs", "allow_custom_job_limit", "job_seconds_ceiling",
                    "job_override_seconds", "job_auto_escalate"})
        request_key(args.get("request_key"))
        integer(args.get("proposal_revision"))
        if "expected_revision" not in args or args["expected_revision"] is not None:
            raise ExecutionError("revision_conflict", 409)
        kind = args.get("coverage_kind")
        if kind not in {"task", "project"}:
            raise ExecutionError("invalid_coverage")
        for field in ("allow_long_runs", "auto_escalate"):
            if type(args.get(field)) is not bool:
                raise ExecutionError("explicit_grant_policy_required")
        if "call_ceiling" not in args or "expires_at" not in args:
            raise ExecutionError("explicit_grant_policy_required")
        ceiling = args["call_ceiling"]
        if ceiling is not None:
            integer(ceiling)
        expires = args["expires_at"]
        if expires is not None:
            try:
                expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if expires.utcoffset() is None:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ExecutionError("invalid_expiry") from None
        for field in ('allow_extended_jobs', 'allow_custom_job_limit', 'job_auto_escalate'):
            if field in args and type(args[field]) is not bool:
                raise ExecutionError('invalid_grant_policy')
        if args.get('job_seconds_ceiling') is not None:
            integer(args['job_seconds_ceiling'])
        if args.get('job_override_seconds') is not None:
            from .job_policy import seconds
            seconds(args['job_override_seconds'])
        if args.get('allow_extended_jobs') and not args['allow_long_runs']:
            raise ExecutionError('extended_job_requires_long_runs')
        async with self.transaction(exclusive_grants=True) as conn:
            previous = await self._mutation(conn, auth, "grants.issue", args["request_key"], args)
            if previous:
                return previous["result"]
            if expires and await conn.fetchval("SELECT $1::timestamptz<=clock_timestamp()", expires):
                raise ExecutionError("invalid_expiry")
            proposal = await self._proposal(conn, auth, args.get("proposal_id"))
            if proposal["revision"] != args["proposal_revision"] or (proposal["consumed"] and not proposal["existing_run"]):
                raise ExecutionError("revision_conflict", 409)
            initiating = Authority(**proposal["authority"])
            from .owner import owner_mapping, check_epoch
            if owner_mapping(initiating.owner):
                await check_epoch(initiating)
            team = await self._team(conn, initiating, proposal["request"]["team"], proposal["request"]["team_revision"])
            head = await conn.fetchval("SELECT revision FROM agent_team_heads WHERE lane=$1 AND owner=$2 AND id=$3",
                                       auth.lane, auth.owner, team["id"])
            if team != proposal["snapshot"]["team"] or head != team["revision"]:
                raise ExecutionError("proposal_stale", 409)
            project = proposal["request"].get("project_id")
            if kind == "project" and not project:
                raise ExecutionError("project_required")
            job_scope = proposal["request"].get("job_coverage")
            job_fields = ("allow_extended_jobs", "allow_custom_job_limit", "job_seconds_ceiling", "job_override_seconds")
            if any(args.get(k) for k in job_fields) and not job_scope:
                raise ExecutionError("job_coverage_required", 409)
            if args.get("job_override_seconds") is not None:
                from .job_policy import authorize
                authorize(args["job_override_seconds"], args)
            grant_id = str(uuid.uuid4())
            await conn.execute("INSERT INTO agent_execution_grants(lane,owner,id,run_id,project_id,authority_boundary,task_digest,team_digest,models_digest,"
                               "allow_long_runs,auto_escalate,call_ceiling,expires_at,matcher_version,coverage_kind,proposal_id,credential_epoch) "
                               "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,2,$14,$15,$16)",
                               auth.lane, auth.owner, grant_id, proposal["run_id"] if kind == "task" else None, project,
                               proposal["boundary"], task_digest(proposal["request"]), digest(proposal["snapshot"]["team"]),
                               digest(proposal["snapshot"]["models"]), args["allow_long_runs"], args["auto_escalate"], ceiling, expires, kind, proposal["id"], auth.credential_epoch)
            if job_scope:
                await conn.execute("UPDATE agent_execution_grants SET matcher_version=3,job_coverage=$4::jsonb,allow_extended_jobs=$5,allow_custom_job_limit=$6,job_seconds_ceiling=$7,job_override_seconds=$8,job_auto_escalate=$9 WHERE lane=$1 AND owner=$2 AND id=$3",
                    auth.lane, auth.owner, grant_id, canonical(job_scope), args.get("allow_extended_jobs", False), args.get("allow_custom_job_limit", False), args.get("job_seconds_ceiling"), args.get("job_override_seconds"), args.get("job_auto_escalate", True))
            grant = public_grant(await conn.fetchrow("SELECT * FROM agent_execution_grants WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, grant_id))
            await conn.execute("UPDATE agent_admission_intents SET grant_id=$4,grant_revision=1 WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, proposal["id"], grant_id)
            if proposal["existing_run"]:
                keys = (auth.lane, auth.owner, proposal["run_id"])
                await self._lock_pools(conn, keys)
                run = await self._row(conn, auth, proposal["run_id"], lock=True)
                if run["boundary"] != proposal["boundary"] or run["authorization_revoked"]:
                    raise ExecutionError("authorization_revoked", 409)
                if run["selected_grant_id"]:
                    # Replacements require a distinct reviewed attach mutation;
                    # don't smuggle attachment/replacement into issuance.
                    raise ExecutionError("replacement_grant_attachment_unavailable", 409)
                await conn.execute("UPDATE agent_runs SET selected_grant_id=$4,selected_grant_revision=1 WHERE lane=$1 AND owner=$2 AND id=$3", *keys, grant_id)
                await self._event(conn, keys, "grant_bound", {"grant_id": grant_id, "grant_revision": 1})
            await conn.execute("INSERT INTO agent_grant_versions(lane,owner,id,revision,record,actor,issuer_authority) VALUES ($1,$2,$3,1,$4::jsonb,$5,$6::jsonb)",
                               auth.lane, auth.owner, grant_id, canonical(grant), auth.actor, canonical(auth.as_dict()))
            return await self._remember(conn, auth, "grants.issue", args["request_key"], args, grant_id, {"grant": grant})

    async def get_grant(self, auth, grant_id):
        async with self.transaction() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_execution_grants WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, text(grant_id, 128))
            if not row:
                raise ExecutionError("not_found", 404)
            return {"grant": public_grant(row)}

    async def list_grants(self, auth, args):
        only(args, {"cursor", "limit"})
        limit = integer(args.get("limit", 20), 1, 50)
        async with self.transaction() as conn:
            rows = await conn.fetch("SELECT * FROM agent_execution_grants WHERE lane=$1 AND owner=$2 AND id>$3 ORDER BY id LIMIT $4",
                                    auth.lane, auth.owner, args.get("cursor") or "", limit + 1)
            return {"items": [public_grant(r) for r in rows[:limit]], "next_cursor": rows[limit-1]["id"] if len(rows)>limit else None}

    async def revoke_grant(self, auth, args):
        only(args, {"grant_id", "expected_revision", "request_key"})
        request_key(args.get("request_key"))
        integer(args.get("expected_revision"))
        async with self.transaction(exclusive_grants=True) as conn:
            previous = await self._mutation(conn, auth, "grants.revoke", args["request_key"], args)
            if previous:
                return previous["result"]
            row = await conn.fetchrow("SELECT * FROM agent_execution_grants WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE",
                                      auth.lane, auth.owner, text(args.get("grant_id"), 128))
            if not row:
                raise ExecutionError("not_found", 404)
            if row["revision"] != args["expected_revision"]:
                raise ExecutionError("revision_conflict", 409)
            grant = public_grant(await conn.fetchrow("UPDATE agent_execution_grants SET revoked=TRUE,revision=revision+1 WHERE lane=$1 AND owner=$2 AND id=$3 RETURNING *",
                                                     auth.lane, auth.owner, row["id"]))
            affected = []
            for raw in await conn.fetch("SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND selected_grant_id=$3 AND status NOT IN ('completed','cancelled','failed') ORDER BY id", auth.lane, auth.owner, row["id"]):
                from .store import record
                run = record(raw)
                keys = (auth.lane, auth.owner, run["id"])
                await self._lock_pools(conn, keys)
                run = await self._row(conn, auth, run["id"], lock=True)
                pending = await conn.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'", *keys)
                if pending:
                    await conn.execute("UPDATE agent_attempts SET state='unknown' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'", *keys)
                    await conn.execute("UPDATE agent_budget_accounts SET unknown_calls=unknown_calls+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys, pending)
                worker_pending = await conn.fetchval("SELECT count(*) FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome IN ('dispatch_intent','running')", *keys)
                await conn.execute("UPDATE worker_job_attempts SET cancel_requested=TRUE WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome IN ('queued','dispatch_intent','running')", *keys)
                cleanup_pending = bool(pending or worker_pending)
                await conn.execute("UPDATE agent_runs SET cleanup_pending=$4 WHERE lane=$1 AND owner=$2 AND id=$3", *keys, cleanup_pending)
                await self._pause(conn, run, "grant_revoked", owner_flag=True, detail={"cleanup_pending": cleanup_pending})
                affected.append({"run_id": run["id"], "cleanup_pending": cleanup_pending})
            await conn.execute("INSERT INTO agent_grant_versions(lane,owner,id,revision,record,actor,issuer_authority) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb)",
                               auth.lane, auth.owner, row["id"], grant["revision"], canonical(grant), auth.actor, canonical(auth.as_dict()))
            return await self._remember(conn, auth, "grants.revoke", args["request_key"], args, row["id"], {"grant": grant, "affected_runs": affected})
