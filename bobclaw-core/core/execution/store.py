"""Postgres authority for teams, admission and fenced checkpoints.

Each state change, charge, output and event commits together. Bounded text outputs
live in Postgres so there is no filesystem/DB publication window in this slice.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import uuid

from .models import Authority, ExecutionError, LOOP_TOPOLOGY, ROLES, TERMINAL, canonical, digest
from .budgets import Budgets
from .loop_store import LoopStore
from .grants import Grants
from .policy import Policies
from .failure_store import FailureStore
from .workflow_store import WorkflowStore

MIGRATION = Path(__file__).resolve().parents[3] / "migrations/2026-11-03_agent_runs.sql"
# Applied in this order by core startup. Packaged first boot applies migrations
# in filename order, so these names must also sort in this order.
EXECUTION_MIGRATIONS = ("2026-11-03_agent_runs.sql", "2026-11-03_agent_runs_loop.sql",
                        "2026-11-03_owner_grants.sql", "2026-11-03_settings_policy.sql",
                        "2026-11-03_worker_queue.sql", "2026-11-03_worker_time_ladder.sql",
                        "2026-11-03_z_failure_recovery.sql", "2026-11-04_tool_jobs.sql",
                        "2026-11-04_z_checked_edits.sql", "2026-11-05_day_one.sql",
                        "2026-11-06_builder_sessions.sql", "2026-11-07_builder_network.sql",
                        "2026-11-08_builder_workspace.sql", "2026-11-09_builder_task.sql", "2026-11-10_pc_requests.sql",
                        "2026-11-16_workflows.sql", "2026-11-16_workflows_controller.sql",
                        "2026-11-16_z_chat_turn_rows.sql", "2026-11-17_step_questions.sql")


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def record(row):
    result = dict(row)
    for key in ("definition", "snapshot", "request", "run_authority", "usage", "detail", "binding", "result",
                "loop_state", "spec", "checks", "criteria_ids", "check_receipts", "initial_budget"):
        if key in result and result[key] is not None:
            result[key] = decoded(result[key])
    return result


class Store(Budgets, LoopStore, Grants, Policies, FailureStore, WorkflowStore):
    def __init__(self, pool, lane):
        self.pool, self.lane = pool, lane

    @asynccontextmanager
    async def transaction(self, *, exclusive_grants=False, existing=None):
        if existing is not None:
            async with existing.transaction():
                yield existing
            return
        if self.pool is None:
            raise ExecutionError("execution_storage_unavailable", 503)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Shared admission/dispatch fence; policy.set and revoke/issue
                # take this SAME key exclusively before queue, pool or run locks.
                # JobStore follows fence -> worker-queue -> rows, including
                # continuation's outer transaction. Never upgrade after row locks.
                function = "pg_advisory_xact_lock" if exclusive_grants else "pg_advisory_xact_lock_shared"
                await conn.execute("SELECT " + function + "($1::bigint)", int(digest([self.lane, "grant-fence"])[:15], 16))
                yield conn

    async def initialize(self):
        async with self.transaction(exclusive_grants=True) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", 0x424F42534348454D)
            for name in EXECUTION_MIGRATIONS:
                await conn.execute(MIGRATION.with_name(name).read_text(encoding="utf-8"))

    async def available(self):
        try:
            async with self.transaction() as conn:
                await conn.fetchval("SELECT 1 FROM agent_runs LIMIT 1")
            return True
        except Exception:
            return False

    async def _mutation(self, conn, auth, op, key, payload):
        # Serializes both present and not-yet-present keys across processes.
        lock = int(digest([auth.lane, auth.owner, op, key])[:15], 16)
        await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", lock)
        row = await conn.fetchrow(
            "SELECT * FROM agent_mutations WHERE lane=$1 AND owner=$2 AND operation=$3 AND request_key=$4",
            auth.lane, auth.owner, op, key)
        if row:
            if row["boundary"] != auth.boundary:
                raise ExecutionError("not_found", 404)
            if row["payload_hash"] != digest(payload):
                raise ExecutionError("idempotency_conflict", 409)
            return record(row)

    async def _remember(self, conn, auth, op, key, payload, resource, result):
        operation_id = str(uuid.uuid4())
        result = {**result, "operation_id": operation_id}
        await conn.execute(
            "INSERT INTO agent_mutations VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)",
            auth.lane, auth.owner, op, key, auth.boundary, digest(payload), resource,
            operation_id, canonical(result))
        return result

    async def replay(self, auth, op, key, payload):
        async with self.transaction() as conn:
            row = await self._mutation(conn, auth, op, key, payload)
            if row:
                return row["result"]

    async def save_team(self, auth, definition, expected, key, payload):
        async with self.transaction() as conn:
            previous = await self._mutation(conn, auth, "teams.save", key, payload)
            if previous:
                return previous["result"]
            # Serialize revision creation even when no head exists yet.
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)",
                               int(digest([auth.lane, auth.owner, definition["id"]])[:15], 16))
            head = await conn.fetchrow(
                "SELECT * FROM agent_team_heads WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE",
                auth.lane, auth.owner, definition["id"])
            if head:
                old = await self._team(conn, auth, definition["id"], head["revision"])
                if old["revision"] != expected:
                    raise ExecutionError("revision_conflict", 409)
            elif expected is not None:
                raise ExecutionError("revision_conflict", 409)
            revision = head["revision"] + 1 if head else 1
            await conn.execute(
                "INSERT INTO agent_team_heads VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (lane,owner,id) DO UPDATE SET revision=EXCLUDED.revision",
                auth.lane, auth.owner, definition["id"], revision)
            await conn.execute(
                "INSERT INTO agent_team_versions (lane,owner,id,revision,actor,boundary,definition) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
                auth.lane, auth.owner, definition["id"], revision, auth.actor, auth.boundary, canonical(definition))
            return await self._remember(conn, auth, "teams.save", key, payload, definition["id"],
                                        {"team": {**definition, "revision": revision}, "saved": True,
                                         "revision": revision})

    async def _team(self, conn, auth, team, revision=None):
        row = await conn.fetchrow(
            "SELECT v.* FROM agent_team_versions v JOIN agent_team_heads h USING (lane,owner,id) "
            "WHERE v.lane=$1 AND v.owner=$2 AND v.id=$3 AND v.revision=COALESCE($4::bigint,h.revision)",
            auth.lane, auth.owner, team, revision)
        if not row or not auth.permits(row):
            raise ExecutionError("team_not_found", 404)
        if decoded(row['definition']).get('topology') == 'workflow/v1':
            raise ExecutionError('workflow_contract_required', 409, {'route':'/workflows'})
        return {**decoded(row["definition"]), "revision": row["revision"]}

    async def team(self, auth, team, revision=None):
        async with self.transaction() as conn:
            return await self._team(conn, auth, team, revision)

    async def teams(self, auth, cursor, limit):
        async with self.transaction() as conn:
            rows = await conn.fetch(
                "SELECT v.* FROM agent_team_versions v JOIN agent_team_heads h USING (lane,owner,id,revision) "
                "WHERE v.lane=$1 AND v.owner=$2 AND v.id>$3 AND COALESCE(v.definition->>'topology','')<>'workflow/v1' AND ($4::text IS NULL OR v.boundary=$4) "
                "ORDER BY v.id LIMIT $5", auth.lane, auth.owner, cursor or "",
                auth.boundary if auth.token_type == "agent" else None, limit + 1)
        return [{**decoded(r["definition"]), "revision": r["revision"]} for r in rows[:limit]], (
            rows[limit - 1]["id"] if len(rows) > limit else None)

    async def start(self, auth, request, snapshot, *, connection=None):
        async with self.transaction(existing=connection) as conn:
            previous = await self._mutation(conn, auth, "runs.start", request["request_key"], request)
            if previous:
                return previous["result"]
            # Verify the referenced version and its initiating boundary in the admission transaction.
            await self._team(conn, auth, request["team"], request["team_revision"])
            run_id = str(uuid.uuid4())
            intent = None
            if request.get("intent_id"):
                intent = await self._proposal(conn, auth, request["intent_id"])
                comparable = {k:v for k,v in request.items() if k not in {"intent_id", "request_key"}}
                prepared = {k:v for k,v in intent["request"].items() if k != "request_key"}
                if (intent["consumed"] or intent["existing_run"] or intent["boundary"] != auth.boundary
                        or intent["authority"].get("credential_epoch") != auth.credential_epoch
                        or comparable != prepared or snapshot != intent["snapshot"]):
                    raise ExecutionError("admission_intent_conflict", 409)
                run_id = intent["run_id"]
                await conn.execute("UPDATE agent_admission_intents SET consumed=TRUE WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, intent["id"])
            seconds = request["budget"]["wall_seconds"]
            await conn.execute(
                "INSERT INTO agent_runs (lane,owner,id,actor,boundary,run_authority,team,team_revision,"
                "snapshot,request,status,deadline) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9::jsonb,$10::jsonb,"
                "'accepted',CASE WHEN $11::bigint IS NULL THEN NULL ELSE clock_timestamp()+$11*interval '1 second' END)",
                auth.lane, auth.owner, run_id, auth.actor, auth.boundary, canonical(auth.as_dict()),
                request["team"], request["team_revision"], canonical(snapshot), canonical(request), seconds)
            await conn.execute("INSERT INTO agent_budget_accounts (lane,owner,run_id,call_limit) VALUES ($1,$2,$3,$4)",
                               auth.lane, auth.owner, run_id, request["budget"]["start_calls"])
            await conn.execute("INSERT INTO agent_budget_allocations(lane,owner,run_id,id,allowance) VALUES ($1,$2,$3,'root',$4)",
                               auth.lane, auth.owner, run_id, request["budget"]["start_calls"])
            grant_row = {"lane": auth.lane, "owner": auth.owner, "id": run_id, "boundary": auth.boundary,
                         "request": request, "snapshot": snapshot, "run_authority": auth.as_dict(),
                         "selected_grant_id": intent["grant_id"] if intent else None,
                         "selected_grant_revision": intent["grant_revision"] if intent else None}
            if grant_row["selected_grant_id"]:
                await conn.execute("UPDATE agent_runs SET selected_grant_id=$4,selected_grant_revision=$5 WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, run_id, grant_row["selected_grant_id"], grant_row["selected_grant_revision"])
            grant = await self._grant(conn, grant_row)
            if auth.token_type == "agent" and request.get("project_id") and not grant:
                raise ExecutionError("resource_not_found", 404)
            calls = request["budget"]["start_calls"]
            await conn.execute("UPDATE agent_runs SET initial_budget=$4::jsonb WHERE lane=$1 AND owner=$2 AND id=$3",
                               auth.lane, auth.owner, run_id, canonical(request["budget"]))
            if calls > 200 and (not grant or not grant["allow_long_runs"]):
                raise ExecutionError("confirmation_required", 409, {"blocked_reasons": ["matching_owner_grant_required"]})
            if grant and grant["call_ceiling"] and calls > grant["call_ceiling"]:
                raise ExecutionError("owner_ceiling", 409)
            await conn.execute("INSERT INTO run_job_accounts(lane,owner,run_id,max_job_attempts,job_seconds_budget) VALUES($1,$2,$3,$4,$5)",
                               auth.lane, auth.owner, run_id, request["budget"].get("max_job_attempts", 32), request["budget"].get("job_seconds_budget"))
            await self._admit_pools(conn, auth, request, run_id, calls)
            roles = () if snapshot["team"]["topology"] == LOOP_TOPOLOGY else ROLES
            for ordinal, role in enumerate(roles):
                await conn.execute("INSERT INTO agent_run_steps (lane,owner,run_id,ordinal,role) VALUES ($1,$2,$3,$4,$5)",
                                   auth.lane, auth.owner, run_id, ordinal, role)
            await self._event(conn, (auth.lane, auth.owner, run_id), "accepted", {})
            return await self._remember(conn, auth, "runs.start", request["request_key"], request, run_id,
                                        {"run_id": run_id})

    async def _row(self, conn, auth, run_id, lock=False):
        row = await conn.fetchrow("SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3" +
                                  (" FOR UPDATE" if lock else ""), auth.lane, auth.owner, run_id)
        if not row or not auth.permits(row):
            raise ExecutionError("run_not_found", 404)
        if dict(row).get('topology') == 'workflow/v1':
            raise ExecutionError('workflow_contract_required', 409, {'route':'/workflow-runs/'+run_id})
        return record(row)

    async def _event(self, conn, keys, kind, detail):
        seq = await conn.fetchval(
            "UPDATE agent_runs SET event_seq=event_seq+1,revision=revision+1,updated_at=clock_timestamp() "
            "WHERE lane=$1 AND owner=$2 AND id=$3 RETURNING event_seq", *keys)
        await conn.execute("INSERT INTO agent_run_events (lane,owner,run_id,seq,kind,detail) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                           *keys, seq, kind, canonical(detail))

    async def get(self, auth, run_id, after=0, receipt_cursor=0):
        async with self.transaction() as conn:
            # Hold the root lock while projecting counters/receipts/events, so a
            # concurrent checkpoint cannot mix two committed run revisions.
            row = await self._row(conn, auth, run_id, lock=True)
            return await self._public(conn, row, after, receipt_cursor)

    async def _public(self, conn, row, after=0, receipt_cursor=0):
        keys = (row["lane"], row["owner"], row["id"])
        account = await conn.fetchrow("SELECT * FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        loop = row["snapshot"]["team"]["topology"] == LOOP_TOPOLOGY
        if not loop and receipt_cursor > 4:
            raise ExecutionError("invalid_integer")
        steps = await conn.fetch("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal>=$4 ORDER BY ordinal LIMIT $5", *keys, receipt_cursor if loop else 0, 3 if loop else 4)
        more_receipts = loop and len(steps) > 2
        if loop:
            steps = steps[:2]
        events = await conn.fetch("SELECT * FROM agent_run_events WHERE lane=$1 AND owner=$2 AND run_id=$3 AND seq>$4 ORDER BY seq LIMIT 101", *keys, after)
        receipts = []
        for step in steps:
            if step["state"] != "completed":
                continue
            # Fixed-topology runs retain NULL specs; original text-loop specs
            # omit kind. Both are model steps with their frozen model identity.
            spec = decoded(step["spec"]) or {}
            kind = spec.get("kind", "model_task")
            face = row["snapshot"]["team"]["roles"][step["role"]] if kind == "model_task" else None
            receipts.append({"step": step["ordinal"], "role": step["role"], "output": step["output"],
                             "output_digest": step["output_digest"], "usage": decoded(step["usage"]),
                             "check_receipts": decoded(step["checks"]), "kind": kind,
                             "model_face": face,
                             "model_pin": row["snapshot"]["models"][face]["model"] if face is not None else None})
        jobs = await conn.fetchrow("SELECT * FROM run_job_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        request = row["request"]
        allocation = await conn.fetchrow("SELECT * FROM agent_budget_allocations WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id='root'", *keys)
        outputs = [record(o) for o in await conn.fetch("SELECT * FROM agent_accepted_outputs WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY ordinal", *keys)]
        manifest = [{"output_id": o["id"], "digest": o["digest"], "step": o["ordinal"], "step_lineage": o["check_receipts"].get("step_lineage", []), "criteria_ids": o["criteria_ids"], "check_receipts": o["check_receipts"]} for o in outputs]
        holds = [dict(h) for h in await conn.fetch("SELECT kind,pool_id,remaining FROM agent_pool_holds WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY kind,pool_id", *keys)]
        growth = [record(e) for e in await conn.fetch("SELECT seq,detail FROM agent_run_events WHERE lane=$1 AND owner=$2 AND run_id=$3 AND kind='budget_grown' ORDER BY seq", *keys)]
        satisfied = {c for o in outputs for c in o["criteria_ids"]}
        unmet = sum(c.get("required", True) and c["id"] not in satisfied for c in request["acceptance"]["criteria"]) if loop else 0
        completed_steps = await conn.fetchval("SELECT count(*) FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='completed'", *keys)
        run = {"id": row["id"], "contract_version": 2, "team": row["team"], "team_revision": row["team_revision"],
               "topology": row["snapshot"]["team"]["topology"], "team_snapshot": row["snapshot"]["team"],
               "worker_contract": row['request'].get('worker_contract'),
               "task": request["task"], "evidence_ids": request.get("evidence_ids", []),
               "acceptance": request.get("acceptance"), "actor": row["actor"],
               "status": row["status"], "reason": row["reason"], "revision": row["revision"],
               "selected_grant_id": row["selected_grant_id"], "selected_grant_revision": row["selected_grant_revision"],
               "cleanup_pending": row["cleanup_pending"],
               "phase": row["phase"] if row["status"] == "running" else row["status"],
               "event_seq": row["event_seq"], "budget": request["budget"],
               "requested_start_calls": request["budget"]["start_calls"],
               "initial_budget": row["initial_budget"],
               "effective_call_limit": account["call_limit"], "calls_used": account["consumed"],
               "job_account": {k: v for k, v in dict(jobs).items() if k not in {"lane", "owner", "run_id"}} if jobs else None,
               "reserved_calls": allocation["reserved"], "unknown_calls": account["unknown_calls"],
               "planned_calls": None if loop else 4,
               "estimated_remaining_calls": None if request.get('worker_contract') else unmet if loop else 4 - completed_steps,
               "remaining_calls_basis": "typed_steps_not_a_model_call_forecast" if request.get('worker_contract') else "unmet_required_criteria_proxy_excludes_planning_may_share_one_call" if loop else "four_step_topology",
               "unmet_required_criteria": unmet if loop else None,
               "accepted_outputs": manifest, "accepted_output_count": len(manifest), "growth_events": growth,
               "pause_reason": row["reason"] if row["status"] == "paused" else None,
               "owner_flag": row["owner_flag"], "pool_holds": holds,
               "completed_steps": completed_steps, "wall_rule_seconds_per_call": row["wall_rule_seconds"],
               "max_model_calls": account["call_limit"], "max_seconds": request["budget"]["wall_seconds"],
               "effective_wall_seconds": int((row["deadline"] - row["created_at"]).total_seconds()) if row["deadline"] else None,
               "receipts": receipts, "independently_verified": False,
               "review_independence": row["snapshot"]["review_independence"],
               "result": (outputs[-1]["content"] if outputs else None) if loop and row["status"] == "completed" else (receipts[-1]["output"] if not loop and len(receipts) == 4 else None)}
        run["flags"] = [self._flag_summary(item) for item in
                        [dict(f) for f in await conn.fetch("SELECT * FROM agent_flags WHERE lane=$1 AND owner=$2 AND run_id=$3 AND status='open' ORDER BY id LIMIT 50", *keys)]]
        run["failure_recovery"] = [{**dict(e), "retry_at": e["retry_at"].isoformat() if e["retry_at"] else None}
                                   for e in await conn.fetch("SELECT id,ordinal,state,basic_retries,audit_calls,guided_calls,retry_at,effect_class,effect_status,auditor FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY created_at LIMIT 50", *keys)]
        run["failure_recovery_truncated"] = await conn.fetchval("SELECT count(*)>50 FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        for key in ("created_at", "updated_at", "deadline"):
            run[key] = row[key].isoformat() if row[key] else None
        for key in ("project_id", "conversation_id"):
            run[key] = request.get(key)
        return {"run": run, "events": [{"seq": e["seq"], "kind": e["kind"], "detail": decoded(e["detail"]),
                                          "at": e["created_at"].isoformat()} for e in events[:100]],
                "receipts": receipts if loop else receipts[receipt_cursor:],
                "outputs": [{k: o[k] for k in ("id", "ordinal", "digest", "content", "criteria_ids", "check_receipts")} for o in outputs if o["ordinal"] >= receipt_cursor][:2],
                "next_receipt_cursor": steps[-1]["ordinal"] + 1 if more_receipts else None,
                "next_cursor": events[99]["seq"] if len(events) > 100 else None}

    async def runs(self, auth, cursor, limit, project=None, status=None):
        async with self.transaction() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id>$3 AND topology<>'workflow/v1' "
                "AND ($4::text IS NULL OR boundary=$4) AND ($5::text IS NULL OR request->>'project_id'=$5) "
                "AND ($6::text IS NULL OR status=$6) ORDER BY id LIMIT $7 FOR SHARE", auth.lane, auth.owner, cursor or "",
                auth.boundary if auth.token_type == "agent" else None, project, status, limit + 1)
            items = []
            for raw in rows[:limit]:
                run = (await self._public(conn, record(raw)))["run"]
                # Lists are summaries. Full outputs belong to runs.get; fifty
                # complete transcripts would exceed the broker's 1 MiB frame.
                run["task_excerpt"] = run["task"][:200]
                for key in ("receipts", "task", "team_snapshot", "result", "acceptance", "evidence_ids", "accepted_outputs", "growth_events"):
                    run.pop(key, None)
                items.append(run)
        return {"items": items, "next_cursor": rows[limit-1]["id"] if len(rows) > limit else None,
                "source": "server", "coverage": "authorized durable v2 runs; legacy gateway JSON excluded"}

    async def cancel(self, auth, run_id, key):
        payload = {"run_id": run_id}
        async with self.transaction() as conn:
            from .job_store import JobStore
            await conn.execute('SELECT pg_advisory_xact_lock($1)',JobStore(self.pool,self.lane).lock)
            await self._lock_pools(conn, (auth.lane, auth.owner, run_id))
            row = await self._row(conn, auth, run_id, lock=True)
            previous = await self._mutation(conn, auth, "runs.cancel", key, payload)
            if previous:
                return previous["result"]
            if row["status"] not in TERMINAL:
                pending = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (outcome IN ('queued','dispatch_intent','running') OR cleanup<>'idle_proved'))",auth.lane,auth.owner,run_id)
                await conn.execute("UPDATE worker_job_attempts SET cancel_requested=TRUE WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome IN ('queued','dispatch_intent','running')",auth.lane,auth.owner,run_id)
                status = "cancelling" if row["controller"] or pending else "cancelled"
                await conn.execute("UPDATE agent_runs SET status=$4,reason='owner_cancelled' WHERE lane=$1 AND owner=$2 AND id=$3",
                                   auth.lane, auth.owner, run_id, status)
                if pending:
                    await conn.execute('UPDATE agent_runs SET cleanup_pending=TRUE WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,run_id)
                await self._event(conn, (auth.lane, auth.owner, run_id), status, {})
                if status == "cancelled":
                    await self._refund_unsent(conn, (auth.lane, auth.owner, run_id))
                    await self._release_holds(conn, (auth.lane, auth.owner, run_id))
            return await self._remember(conn, auth, "runs.cancel", key, payload, run_id, {"run_id": run_id})

    async def claim(self, controller, *, workflows=False):
        async with self.transaction() as conn:
            # One lane lock makes the two-controller limit deterministic across pumps.
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", int(digest([self.lane, "pump"])[:15], 16))
            active = await conn.fetchval("SELECT count(*) FROM agent_runs WHERE lane=$1 AND controller IS NOT NULL AND lease_until>clock_timestamp() AND (topology<>'workflow/v1' OR status<>'needs_you')", self.lane)
            import os
            if active >= max(1,min(16,int(os.environ.get('BOB_WORKFLOW_RUN_CONTROLLER_LIMIT','2')))):
                return None
            raw = await conn.fetchrow(
                "SELECT * FROM agent_runs WHERE lane=$1 AND (topology<>'workflow/v1' OR $2) "
                "AND (status IN ('accepted','queued','running','recovering','cancelling','pausing','stopping') OR "
                "(topology='workflow/v1' AND status IN ('needs_you','paused') AND grant_expires_at<=clock_timestamp()) OR "
                "(topology='workflow/v1' AND status IN ('completed','stopped','expired','failed','blocked','unknown') "
                "AND workflow_state->'run'->'report'->>'assistant_summary'='pending')) "
                "AND (lease_until IS NULL OR lease_until<=clock_timestamp()) ORDER BY updated_at,created_at LIMIT 1", self.lane, workflows)
            if not raw:
                return None
            row = record(raw)
            keys = (row["lane"], row["owner"], row["id"])
            if row.get('topology') == 'workflow/v1':
                return await self.wf_claim(conn, row, controller)
            await self._lock_pools(conn, keys)
            row = record(await conn.fetchrow("SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE", *keys))
            if row["status"] not in {"accepted", "queued", "running", "recovering", "cancelling"} or (row["lease_until"] and await conn.fetchval("SELECT $1::timestamptz>clock_timestamp()", row["lease_until"])):
                return None
            if row['status'] == 'cancelling':
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (outcome NOT IN ('succeeded','failed','timed_out','cancelled') OR cleanup<>'idle_proved'))", *keys):
                    return None
            await self._refund_unsent(conn, keys)
            # Unacknowledged dispatched calls remain charged. Never speculate/replay.
            unknown = await conn.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'", *keys)
            if unknown:
                await conn.execute("UPDATE agent_attempts SET state='unknown' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'", *keys)
                await conn.execute("UPDATE agent_budget_accounts SET unknown_calls=unknown_calls+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys, unknown)
            reason = None
            if row["controller"]:
                await self._event(conn, keys, "recovered", {"previous_generation": row["generation"], "unknown_calls": unknown})
            if row["status"] == "cancelling":
                reason, status = "owner_cancelled", "cancelled"
            elif unknown:
                reason, status = "unknown_provider_result", "paused"
            elif row["deadline"] and await conn.fetchval("SELECT $1::timestamptz<=clock_timestamp()", row["deadline"]):
                reason, status = "wall_time_expired", "paused"
            else:
                status = "running"
            await conn.execute(
                "UPDATE agent_runs SET status=$4,reason=$5,generation=generation+1,controller=$6,"
                "lease_until=CASE WHEN $6::text IS NULL THEN NULL ELSE clock_timestamp()+interval '60 seconds' END "
                "WHERE lane=$1 AND owner=$2 AND id=$3", *keys, status, reason, controller if status == "running" else None)
            await self._event(conn, keys, status, {"reason": reason})
            if status != "running":
                if status == "paused":
                    episode = await conn.fetchrow("SELECT id,ordinal FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state NOT IN ('resolved','paused') ORDER BY created_at LIMIT 1", *keys)
                    if episode:
                        await conn.execute("UPDATE agent_failure_episodes SET resume_state=CASE WHEN $5='wall_time_expired' THEN state ELSE NULL END,state='paused' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode["id"], reason)
                    await self._flag(conn, row, reason, episode_id=episode["id"] if episode else None,
                                     ordinal=episode["ordinal"] if episode else None)
                await self._release_holds(conn, keys)
                return None
            current = record(await conn.fetchrow("SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3", *keys))
            try:
                await self._dispatchable(conn, current)
            except ExecutionError as exc:
                await self._pause(conn, current, exc.code, owner_flag=True)
                return None
            return current

    async def _fenced(self, conn, row):
        current = await conn.fetchrow("SELECT *,lease_until>clock_timestamp() AS live FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE",
                                      row["lane"], row["owner"], row["id"])
        if (not current or current["generation"] != row["generation"] or current["controller"] != row["controller"] or not current["live"]):
            raise ExecutionError("stale_controller", 409)
        return record(current)

    async def heartbeat(self, row):
        async with self.transaction() as conn:
            current = await self._fenced(conn, row)
            if current["status"] != "running":
                return current["status"]
            if current["authorization_revoked"]:
                return "authorization_revoked"
            if current["deadline"] and await conn.fetchval("SELECT $1::timestamptz<=clock_timestamp()", current["deadline"]):
                return "deadline"
            await conn.execute("UPDATE agent_runs SET lease_until=clock_timestamp()+interval '60 seconds' WHERE lane=$1 AND owner=$2 AND id=$3",
                               row["lane"], row["owner"], row["id"])
            return "running"

    async def steps(self, row):
        async with self.transaction() as conn:
            return [record(r) for r in await conn.fetch("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY ordinal",
                                                       row["lane"], row["owner"], row["id"])]

    async def dispatch(self, row, ordinal, binding, prompt):
        # Legacy four-step topology shares the exact reservation/charge path.
        async with self.transaction() as conn:
            done = await conn.fetchval("SELECT count(*) FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal<$4 AND state='completed'",
                                       row["lane"], row["owner"], row["id"], ordinal)
            if done != ordinal:
                raise ExecutionError("dependency_not_completed", 409)
        attempt = await self.reserve_attempt(row, ordinal, binding, prompt)
        return await self.charge_attempt(row, attempt)

    async def checkpoint(self, row, ordinal, attempt, output, usage):
        async with self.transaction() as conn:
            await self._lock_pools(conn, (row["lane"], row["owner"], row["id"]))
            current = await self._fenced(conn, row)
            keys = (row["lane"], row["owner"], row["id"])
            if current["status"] != "running":
                return False  # cancellation wins over the late response
            if current["deadline"] and await conn.fetchval("SELECT $1::timestamptz<=clock_timestamp()", current["deadline"]):
                return False
            changed = await conn.fetchval("UPDATE agent_attempts SET state='completed' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND generation=$5 AND state='dispatched' RETURNING id",
                                          *keys, attempt, row["generation"])
            if not changed:
                raise ExecutionError("stale_attempt", 409)
            await self._finish_failure(conn, current, ordinal, attempt, output)
            await conn.execute("UPDATE agent_run_steps SET state='completed',output=$5,output_digest=$6,usage=$7::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4",
                               *keys, ordinal, output, digest(output), canonical(usage))
            await self._event(conn, keys, "step_completed", {"step": ordinal, "attempt_id": attempt, "output_digest": digest(output)})
            if ordinal == 3 and not await conn.fetchval("SELECT 1 FROM tool_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys):
                await self._release_holds(conn, keys)
                await conn.execute("UPDATE agent_runs SET status='completed',controller=NULL,lease_until=NULL WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
                await self._event(conn, keys, "completed", {})
            return True

    async def settle_revocation(self, row):
        """Called only after this controller closed its text transport task.

        This acknowledges local transport cleanup, never a provider result or
        worker descendant. Worker jobs remain unavailable in this contract.
        """
        async with self.transaction() as conn:
            keys = (row["lane"], row["owner"], row["id"])
            current = await conn.fetchrow("SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE", *keys)
            if not current or current["generation"] != row["generation"] or current["reason"] not in {"grant_revoked", "policy_narrowed"}:
                return False
            if current["cleanup_pending"]:
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (outcome IN ('queued','dispatch_intent','running') OR cleanup<>'idle_proved'))", *keys):
                    return True
                await conn.execute("UPDATE agent_runs SET cleanup_pending=FALSE WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
                await self._event(conn, keys, "revocation_transport_closed", {"unknown_effects_remain_charged": True})
            return True

    async def stop(self, row, reason, *, unknown=True):
        async with self.transaction() as conn:
            await self._lock_pools(conn, (row["lane"], row["owner"], row["id"]))
            current = await self._fenced(conn, row)
            keys = (row["lane"], row["owner"], row["id"])
            attempts = await conn.fetch("UPDATE agent_attempts SET state=$4 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched' RETURNING id",
                                        *keys, "unknown" if unknown else "failed")
            if attempts and unknown:
                await conn.execute("UPDATE agent_budget_accounts SET unknown_calls=unknown_calls+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys, len(attempts))
            await self._refund_unsent(conn, keys)
            await self._release_holds(conn, keys)
            status = "cancelled" if current["status"] == "cancelling" else "paused"
            if current["status"] == "paused" and current["reason"] == "worker_outcome_unknown":
                reason = current["reason"]
            await conn.execute("UPDATE agent_runs SET status=$4,reason=$5,controller=NULL,lease_until=NULL WHERE lane=$1 AND owner=$2 AND id=$3", *keys, status, reason)
            await self._event(conn, keys, status, {"reason": reason, "unknown_calls": len(attempts) if unknown else 0})
            if status == "paused" and reason != "worker_outcome_unknown":
                # Associate control failures (missing auditor, deadline, etc.)
                # with the existing episode instead of manufacturing a new flag.
                episode = await conn.fetchrow("SELECT id,ordinal FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state NOT IN ('resolved','paused') ORDER BY created_at LIMIT 1", *keys)
                if episode:
                    await conn.execute("UPDATE agent_failure_episodes SET resume_state=CASE WHEN $5 IN ('call_budget_exhausted','wall_time_expired') THEN state ELSE NULL END,state='paused' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode["id"], reason)
                await self._flag(conn, current, reason, episode_id=episode["id"] if episode else None,
                                 ordinal=episode["ordinal"] if episode else None)

    async def release(self, row):
        """Graceful restart at a checkpoint: completed steps remain reusable."""
        async with self.transaction() as conn:
            current = await self._fenced(conn, row)
            keys = (row["lane"], row["owner"], row["id"])
            await conn.execute("UPDATE agent_runs SET lease_until=clock_timestamp() WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
            await self._event(conn, keys, "interrupted", {"reason": "core_shutdown", "status": current["status"]})
