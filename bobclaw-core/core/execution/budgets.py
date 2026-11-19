"""Atomic call accounting. Pools are locked in stable order before the run root.

Every descendant shares that root lock. Transferring credits increases delegated
capacity, not total allowance. Reservations are durable and refundable only while
proven unsent; charging moves pool holds to consumption before any transport.
"""
from __future__ import annotations

from datetime import timedelta
import uuid

from .models import ExecutionError, canonical, digest, integer, only, request_key, text


def next_limit(current):
    for preset in (10, 50, 100, 200):
        if preset > current:
            return preset
    return integer(current * 2)


def task_digest(request):
    return digest({k: request.get(k) for k in ("task", "evidence_ids", "acceptance")})


def grown_deadline(row, limit):
    if row["request"]["budget"]["wall_mode"] != "follow_budget":
        return row["deadline"]
    try:
        proposed = row["created_at"] + timedelta(seconds=limit * row["wall_rule_seconds"])
    except OverflowError:
        raise ExecutionError("wall_time_overflow", 409) from None
    return max(row["deadline"], proposed) if row["deadline"] else proposed


class Budgets:
    async def _budget_projection(self, conn, auth, project_id):
        row = await conn.fetchrow("SELECT * FROM agent_call_pools WHERE lane=$1 AND owner=$2 AND kind='project' AND id=$3",
                                  auth.lane, auth.owner, project_id)
        row = dict(row) if row else {"call_limit": None, "consumed": 0, "reserved": 0, "revision": None,
                                    "counter_coverage": "durable_runs_only"}
        observed = await conn.fetchval("SELECT clock_timestamp()")
        return {"project_id": project_id, "configured": row["call_limit"] is not None,
                **{key: row[key] for key in ("call_limit", "consumed", "reserved", "revision", "counter_coverage")},
                "remaining": row["call_limit"] - row["consumed"] - row["reserved"] if row["call_limit"] is not None else None,
                "observed_at": observed.isoformat()}

    async def get_budget(self, auth, project_id):
        text(project_id, 128)
        async with self.transaction() as conn:
            return await self._budget_projection(conn, auth, project_id)

    async def list_budgets(self, auth, args):
        only(args, {"cursor", "limit", "kind"})
        if args.get("kind", "project") != "project":
            raise ExecutionError("unsupported_budget_kind")
        limit = integer(args.get("limit", 20), 1, 50)
        async with self.transaction() as conn:
            rows = await conn.fetch("SELECT id FROM agent_call_pools WHERE lane=$1 AND owner=$2 AND kind='project' AND id>$3 ORDER BY id LIMIT $4",
                                    auth.lane, auth.owner, args.get("cursor") or "", limit + 1)
            return {"items": [await self._budget_projection(conn, auth, row["id"]) for row in rows[:limit]],
                    "next_cursor": rows[limit-1]["id"] if len(rows)>limit else None}

    async def remove_budget(self, auth, args):
        only(args, {"project_id", "expected_revision", "request_key"})
        text(args.get("project_id"), 128)
        request_key(args.get("request_key"))
        integer(args.get("expected_revision"))
        async with self.transaction() as conn:
            previous = await self._mutation(conn, auth, "budgets.remove", args["request_key"], args)
            if previous:
                return previous["result"]
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", int(digest([auth.lane, auth.owner, "project", args["project_id"]])[:15], 16))
            pool = await conn.fetchrow("SELECT * FROM agent_call_pools WHERE lane=$1 AND owner=$2 AND kind='project' AND id=$3 FOR UPDATE", auth.lane, auth.owner, args["project_id"])
            if not pool:
                raise ExecutionError("not_found", 404)
            if pool["revision"] != args["expected_revision"]:
                raise ExecutionError("revision_conflict", 409)
            if pool["reserved"]:
                raise ExecutionError("budget_in_use", 409, {"held": pool["reserved"]})
            await conn.execute("UPDATE agent_call_pools SET call_limit=NULL,revision=revision+1 WHERE lane=$1 AND owner=$2 AND kind='project' AND id=$3", auth.lane, auth.owner, args["project_id"])
            result = await self._budget_projection(conn, auth, args["project_id"])
            return await self._remember(conn, auth, "budgets.remove", args["request_key"], args, args["project_id"], result)

    async def _lock_pools(self, conn, keys):
        return await conn.fetch(
            "SELECT p.* FROM agent_call_pools p JOIN agent_pool_holds h "
            "ON (p.lane,p.owner,p.kind,p.id)=(h.lane,h.owner,h.kind,h.pool_id) "
            "WHERE h.lane=$1 AND h.owner=$2 AND h.run_id=$3 ORDER BY p.kind,p.id FOR UPDATE OF p", *keys)

    async def _admit_pools(self, conn, auth, request, run_id, amount):
        # Serialize absent pools with budgets.set too. No implicit daily/pool cap.
        for kind in ("project", "sprint"):
            pool_id = request.get(kind + "_id")
            if not pool_id:
                continue
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)",
                               int(digest([auth.lane, auth.owner, kind, pool_id])[:15], 16))
            pool = await conn.fetchrow("SELECT * FROM agent_call_pools WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4 FOR UPDATE",
                                       auth.lane, auth.owner, kind, pool_id)
            if not pool:
                if kind != "project":
                    continue
                await conn.execute("INSERT INTO agent_call_pools(lane,owner,kind,id,call_limit) VALUES ($1,$2,$3,$4,NULL)", auth.lane, auth.owner, kind, pool_id)
                pool = {"call_limit": None, "consumed": 0, "reserved": 0}
            remaining = pool["call_limit"] - pool["consumed"] - pool["reserved"] if pool["call_limit"] is not None else None
            if remaining is not None and remaining < amount:
                raise ExecutionError("pool_shortfall", 409, {"kind": kind, "pool_id": pool_id, "remaining": remaining, "requested": amount})
            await conn.execute("UPDATE agent_call_pools SET reserved=reserved+$5 WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4",
                               auth.lane, auth.owner, kind, pool_id, amount)
            await conn.execute("INSERT INTO agent_pool_holds VALUES ($1,$2,$3,$4,$5,$6)",
                               auth.lane, auth.owner, run_id, kind, pool_id, amount)

    async def set_budget(self, auth, args):
        only(args, {"project_id", "call_limit", "expected_revision", "request_key"})
        if auth.token_type != "human" or auth.actor != auth.owner:
            raise ExecutionError("not_found", 404)
        text(args.get("project_id"), 128)
        integer(args.get("call_limit"))
        request_key(args.get("request_key"))
        if "expected_revision" not in args:
            raise ExecutionError("expected_revision_required")
        expected = args["expected_revision"]
        if expected is not None:
            integer(expected)
        async with self.transaction() as conn:
            previous = await self._mutation(conn, auth, "budgets.set", args["request_key"], args)
            if previous:
                return previous["result"]
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)",
                               int(digest([auth.lane, auth.owner, "project", args["project_id"]])[:15], 16))
            pool = await conn.fetchrow("SELECT * FROM agent_call_pools WHERE lane=$1 AND owner=$2 AND kind='project' AND id=$3 FOR UPDATE",
                                       auth.lane, auth.owner, args["project_id"])
            if (pool["revision"] if pool else None) != expected:
                raise ExecutionError("revision_conflict", 409)
            if pool and args["call_limit"] < pool["consumed"] + pool["reserved"]:
                raise ExecutionError("below_committed_budget", 409, {"committed": pool["consumed"] + pool["reserved"]})
            revision = expected + 1 if expected is not None else 1
            await conn.execute(
                "INSERT INTO agent_call_pools(lane,owner,kind,id,call_limit,revision) VALUES ($1,$2,'project',$3,$4,$5) "
                "ON CONFLICT (lane,owner,kind,id) DO UPDATE SET call_limit=EXCLUDED.call_limit,revision=EXCLUDED.revision",
                auth.lane, auth.owner, args["project_id"], args["call_limit"], revision)
            result = await self._budget_projection(conn, auth, args["project_id"])
            return await self._remember(conn, auth, "budgets.set", args["request_key"], args, args["project_id"], result)

    async def _grant(self, conn, row):
        from .grants import grant_matches
        selected = row.get("selected_grant_id")
        grants = await conn.fetch(
            "SELECT * FROM agent_execution_grants WHERE lane=$1 AND owner=$2 AND NOT revoked "
            "AND (expires_at IS NULL OR expires_at>clock_timestamp()) "
            "AND (run_id=$3 OR (run_id IS NULL AND project_id=$4)) AND ($5::text IS NULL OR id=$5) ORDER BY revision DESC,id",
            row["lane"], row["owner"], row["id"], row["request"].get("project_id"), selected)
        for grant in grants:
            if (grant_matches(dict(grant), row) and
                    (not selected or grant["revision"] == row["selected_grant_revision"])):
                if not selected:
                    await conn.execute("UPDATE agent_runs SET selected_grant_id=$4,selected_grant_revision=$5 WHERE lane=$1 AND owner=$2 AND id=$3", row["lane"], row["owner"], row["id"], grant["id"], grant["revision"])
                    row["selected_grant_id"], row["selected_grant_revision"] = grant["id"], grant["revision"]
                return grant
        if selected:
            raise ExecutionError("grant_revoked", 409)

    async def _chain(self, conn, keys, allocation_id):
        rows, seen = [], set()
        while allocation_id is not None:
            if allocation_id in seen or len(rows) > 4:
                raise ExecutionError("invalid_allocation_tree", 409)
            seen.add(allocation_id)
            allocation = await conn.fetchrow("SELECT * FROM agent_budget_allocations WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 FOR UPDATE",
                                              *keys, allocation_id)
            if not allocation:
                raise ExecutionError("allocation_not_found", 409)
            rows.append(allocation)
            allocation_id = allocation["parent_id"]
        return rows

    async def suballocate(self, row, parent_id, allocation_id, amount):
        integer(amount)
        text(allocation_id, 128)
        if allocation_id == parent_id or allocation_id == "root":
            raise ExecutionError("invalid_allocation_tree")
        async with self.transaction() as conn:
            current = await self._fenced(conn, row)
            await self._dispatchable(conn, current)
            keys = (row["lane"], row["owner"], row["id"])
            chain = await self._chain(conn, keys, parent_id)
            if len(chain) >= 5:
                raise ExecutionError("allocation_too_deep")
            parent = chain[0]
            remaining = parent["allowance"] - parent["consumed"] - parent["reserved"] - parent["delegated"]
            if remaining < amount:
                raise ExecutionError("allocation_shortfall", 409, {"remaining": remaining})
            await conn.execute("UPDATE agent_budget_allocations SET delegated=delegated+$5 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, parent_id, amount)
            await conn.execute("INSERT INTO agent_budget_allocations(lane,owner,run_id,id,parent_id,allowance) VALUES ($1,$2,$3,$4,$5,$6)", *keys, allocation_id, parent_id, amount)
            await self._event(conn, keys, "budget_suballocated", {"parent_id": parent_id, "allocation_id": allocation_id, "calls": amount})

    async def reserve_attempt(self, row, ordinal, binding, prompt, allocation_id="root", *, episode_id=None, purpose="initial"):
        async with self.transaction() as conn:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(conn, keys)
            current = await self._fenced(conn, row)
            await self._dispatchable(conn, current)
            step = await conn.fetchrow("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, ordinal)
            if not step or step["state"] != "ready":
                raise ExecutionError("step_not_ready", 409)
            await self._reserve_failure(conn, current, step, binding, episode_id, purpose)
            chain = await self._chain(conn, keys, allocation_id)
            leaf = chain[0]
            remaining = leaf["allowance"] - leaf["consumed"] - leaf["reserved"] - leaf["delegated"]
            if remaining < 1:
                raise ExecutionError("call_budget_exhausted", 409, {"remaining": remaining})
            for index, account in enumerate(chain):
                await conn.execute("UPDATE agent_budget_allocations SET reserved=reserved+1,delegated=delegated-$5 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4",
                                   *keys, account["id"], 0 if index == 0 else 1)
            attempt = str(uuid.uuid4())
            await conn.execute("INSERT INTO agent_attempts(lane,owner,run_id,id,ordinal,generation,binding,input_digest,state,allocation_id) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,'reserved',$9)",
                               *keys, attempt, ordinal, row["generation"], canonical(binding), digest(prompt), allocation_id)
            await conn.execute("UPDATE agent_attempts SET failure_episode_id=$5,purpose=$6 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, attempt, episode_id, purpose)
            await conn.execute("UPDATE agent_run_steps SET state='reserved' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, ordinal)
            await self._event(conn, keys, "reserved", {"attempt_id": attempt, "step": ordinal, "allocation_id": allocation_id})
            return attempt

    async def _dispatchable(self, conn, row):
        await self.check_policy(conn, row)
        if row["status"] != "running":
            raise ExecutionError("run_stopped", 409)
        if row["authorization_revoked"]:
            raise ExecutionError("authorization_revoked", 409)
        from .owner import owner_mapping
        if owner_mapping(row["owner"]) or row["run_authority"].get("credential_epoch") is not None:
            from .models import Authority
            from .owner import check_epoch
            await check_epoch(Authority(**row["run_authority"]))
        limit = await conn.fetchval("SELECT call_limit FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", row["lane"], row["owner"], row["id"])
        grant = await self._grant(conn, row)
        if limit > 200 and (not grant or not grant["allow_long_runs"]):
            raise ExecutionError("grant_revoked", 409)
        if grant and grant["call_ceiling"] and limit > grant["call_ceiling"]:
            raise ExecutionError("owner_ceiling", 409)
        if row["deadline"] and await conn.fetchval("SELECT $1::timestamptz<=clock_timestamp()", row["deadline"]):
            raise ExecutionError("wall_time_expired", 409)

    async def charge_attempt(self, row, attempt):
        async with self.transaction() as conn:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(conn, keys)
            current = await self._fenced(conn, row)
            await self._dispatchable(conn, current)
            item = await conn.fetchrow("SELECT * FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND generation=$5 AND state='reserved' FOR UPDATE", *keys, attempt, row["generation"])
            if not item:
                raise ExecutionError("unreserved_model_call", 409)
            step = await conn.fetchrow("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, item["ordinal"])
            from .store import decoded
            await self._reserve_failure(conn, current, step, decoded(item["binding"]), item["failure_episode_id"], item["purpose"])
            await self._charge_failure(conn, current, item)
            for account in await self._chain(conn, keys, item["allocation_id"]):
                await conn.execute("UPDATE agent_budget_allocations SET reserved=reserved-1,consumed=consumed+1 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, account["id"])
            await conn.execute("UPDATE agent_budget_accounts SET consumed=consumed+1 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            for hold in await conn.fetch("SELECT * FROM agent_pool_holds WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys):
                if hold["remaining"] < 1:
                    raise ExecutionError("pool_shortfall", 409)
                await conn.execute("UPDATE agent_call_pools SET reserved=reserved-1,consumed=consumed+1 WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4", keys[0], keys[1], hold["kind"], hold["pool_id"])
                await conn.execute("UPDATE agent_pool_holds SET remaining=remaining-1 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND kind=$4 AND pool_id=$5", *keys, hold["kind"], hold["pool_id"])
            await conn.execute("UPDATE agent_attempts SET state='dispatched',dispatched_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, attempt)
            await conn.execute("UPDATE agent_run_steps SET state='dispatched' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, item["ordinal"])
            await conn.execute("UPDATE agent_runs SET phase=CASE WHEN $5='audit' THEN 'audit' WHEN $5='guided' THEN 'guided_retry' WHEN (SELECT spec->>'phase' FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4)='plan' THEN 'plan' ELSE 'dispatch' END WHERE lane=$1 AND owner=$2 AND id=$3", *keys, item["ordinal"], item["purpose"])
            await self._event(conn, keys, "dispatched", {"step": item["ordinal"], "attempt_id": attempt})
            return attempt

    async def _refund_unsent(self, conn, keys):
        attempts = await conn.fetch("SELECT * FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='reserved'", *keys)
        for item in attempts:
            for index, account in enumerate(await self._chain(conn, keys, item["allocation_id"])):
                await conn.execute("UPDATE agent_budget_allocations SET reserved=reserved-1,delegated=delegated+$5 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, account["id"], 0 if index == 0 else 1)
            await conn.execute("UPDATE agent_attempts SET state='released' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, item["id"])
            await conn.execute("UPDATE agent_run_steps SET state='ready' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, item["ordinal"])

    async def _release_holds(self, conn, keys):
        for hold in await conn.fetch("SELECT * FROM agent_pool_holds WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys):
            await conn.execute("UPDATE agent_call_pools SET reserved=reserved-$5 WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4", keys[0], keys[1], hold["kind"], hold["pool_id"], hold["remaining"])
        await conn.execute("UPDATE agent_pool_holds SET remaining=0 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)

    async def _raise_limit(self, conn, row, target, reason, grant=None):
        keys = (row["lane"], row["owner"], row["id"])
        account = await conn.fetchrow("SELECT * FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3 FOR UPDATE", *keys)
        old = account["call_limit"]
        if target > 200 and (not grant or not grant["allow_long_runs"]):
            raise ExecutionError("confirmation_required", 409)
        if grant and grant["call_ceiling"] and target > grant["call_ceiling"]:
            raise ExecutionError("owner_ceiling", 409)
        if target < old:
            raise ExecutionError("budget_decrease_not_allowed", 409)
        # A paused run may have released its free hold: reserve exactly the gap.
        for pool in await self._lock_pools(conn, keys):
            hold = await conn.fetchval("SELECT remaining FROM agent_pool_holds WHERE lane=$1 AND owner=$2 AND run_id=$3 AND kind=$4 AND pool_id=$5", *keys, pool["kind"], pool["id"])
            delta = target - account["consumed"] - hold
            remaining = pool["call_limit"] - pool["consumed"] - pool["reserved"] if pool["call_limit"] is not None else None
            if remaining is not None and delta > remaining:
                raise ExecutionError("pool_shortfall", 409, {"kind": pool["kind"], "pool_id": pool["id"], "remaining": remaining, "requested": delta})
            await conn.execute("UPDATE agent_call_pools SET reserved=reserved+$5 WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4", keys[0], keys[1], pool["kind"], pool["id"], delta)
            await conn.execute("UPDATE agent_pool_holds SET remaining=remaining+$6 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND kind=$4 AND pool_id=$5", *keys, pool["kind"], pool["id"], delta)
        deadline = grown_deadline(row, target) if target > old else row["deadline"]
        await conn.execute("UPDATE agent_budget_accounts SET call_limit=$4 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys, target)
        await conn.execute("UPDATE agent_budget_allocations SET allowance=allowance+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id='root'", *keys, target - old)
        await conn.execute("UPDATE agent_runs SET deadline=$4 WHERE lane=$1 AND owner=$2 AND id=$3", *keys, deadline)
        if target > old:
            await self._event(conn, keys, "budget_grown", {"old_limit": old, "new_limit": target, "reason": reason,
                               "deadline": deadline.isoformat() if deadline else None,
                               "grant_id": grant["id"] if grant else None, "grant_revision": grant["revision"] if grant else None})
        return deadline

    async def evaluate_growth(self, row, expected_revision):
        async with self.transaction() as conn:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(conn, keys)
            current = await self._fenced(conn, row)
            if current["revision"] != expected_revision:
                raise ExecutionError("revision_conflict", 409)
            await self._dispatchable(conn, current)
            unsettled = await conn.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state IN ('reserved','dispatched')", *keys)
            if unsettled:
                return "settling"
            account = await conn.fetchrow("SELECT * FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            if account["consumed"] < account["call_limit"]:
                return "available"
            count = await conn.fetchval("SELECT count(*) FROM agent_accepted_outputs WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            reason = "budget_exhausted_with_output" if count else None
            detail = None
            if not reason:
                grant = await self._grant(conn, current)
                if grant and not grant["auto_escalate"]:
                    reason = "owner_ceiling"
                else:
                    try:
                        target = next_limit(account["call_limit"])
                        if grant and grant["call_ceiling"] and target > grant["call_ceiling"]:
                            if grant["call_ceiling"] <= account["call_limit"]:
                                raise ExecutionError("owner_ceiling", 409)
                            target = grant["call_ceiling"]
                        # Savepoint: a pool shortfall must not retain any earlier pool update.
                        async with conn.transaction():
                            row["deadline"] = await self._raise_limit(conn, current, target, "no_accepted_output", grant)
                        return "grown"
                    except ExecutionError as exc:
                        reason = exc.code
                        detail = exc.detail
            await self._pause(conn, current, reason, owner_flag=True, detail=detail)
            return reason

    async def _pause(self, conn, row, reason, owner_flag=False, detail=None, ordinal=None):
        keys = (row["lane"], row["owner"], row["id"])
        unknown = await conn.fetch("UPDATE agent_attempts SET state='unknown' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched' RETURNING id", *keys)
        if unknown:
            await conn.execute("UPDATE agent_budget_accounts SET unknown_calls=unknown_calls+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys, len(unknown))
        await self._refund_unsent(conn, keys)
        await self._release_holds(conn, keys)
        await conn.execute("UPDATE agent_runs SET status='paused',reason=$4,owner_flag=$5,controller=NULL,lease_until=NULL WHERE lane=$1 AND owner=$2 AND id=$3", *keys, reason, owner_flag)
        await self._event(conn, keys, "paused", {"reason": reason, "owner_flag": owner_flag, "detail": detail})
        if owner_flag:
            # Pause every active episode even if a different flag is already open.
            # Policy narrowing may suspend a safe pending recovery, but cannot
            # make an uncertain dispatched call replayable or reset its counts.
            episodes = await conn.fetch("SELECT id,ordinal FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state NOT IN ('resolved','paused') ORDER BY created_at", *keys)
            for episode in episodes:
                await conn.execute("UPDATE agent_failure_episodes SET resume_state=CASE WHEN $5='policy_narrowed' AND effect_status IN ('none','not_applied') AND NOT EXISTS (SELECT 1 FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='unknown') THEN state ELSE NULL END,state='paused',updated_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode["id"], reason)
                await self._flag(conn, row, reason, episode_id=episode["id"], ordinal=episode["ordinal"])
            existing = await conn.fetchval("SELECT 1 FROM agent_flags WHERE lane=$1 AND owner=$2 AND run_id=$3 AND status='open' LIMIT 1", *keys)
            if not existing:
                await self._flag(conn, row, reason, ordinal=ordinal)

    async def resume(self, auth, args):
        only(args, {"run_id", "expected_revision", "request_key", "budget"})
        if auth.token_type != "human" or auth.actor != auth.owner:
            raise ExecutionError("not_found", 404)
        request_key(args.get("request_key"))
        integer(args.get("expected_revision"))
        async with self.transaction() as conn:
            previous = await self._mutation(conn, auth, "runs.resume", args["request_key"], args)
            if previous:
                return previous["result"]
            keys = (auth.lane, auth.owner, args.get("run_id"))
            await self._lock_pools(conn, keys)
            row = await self._row(conn, auth, args.get("run_id"), lock=True)
            if row["revision"] != args["expected_revision"]:
                raise ExecutionError("revision_conflict", 409)
            if row["status"] != "paused" or row["reason"] not in {
                "call_budget_exhausted", "budget_exhausted_with_output", "confirmation_required", "owner_ceiling",
                "pool_shortfall", "no_progress", "wall_time_expired", "policy_narrowed"}:
                raise ExecutionError("run_not_resumable", 409)
            if row["authorization_revoked"]:
                raise ExecutionError("authorization_revoked", 403)
            await self.check_policy(conn, row)
            if await conn.fetchval("SELECT count(*) FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='paused' AND resume_state IS NULL", *keys):
                raise ExecutionError("run_not_resumable", 409)
            from .owner import owner_mapping
            if owner_mapping(row["owner"]) or row["run_authority"].get("credential_epoch") is not None:
                from .models import Authority
                from .owner import check_epoch
                await check_epoch(Authority(**row["run_authority"]))
            if await conn.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state IN ('reserved','dispatched','unknown')", *keys):
                raise ExecutionError("unsettled_attempts", 409)
            account = await conn.fetchrow("SELECT * FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            proposal = args.get("budget", {})
            only(proposal, {"start_calls", "wall_mode", "wall_seconds"})
            target = integer(proposal.get("start_calls", account["call_limit"]))
            if target <= account["consumed"]:
                raise ExecutionError("insufficient_call_allowance", 409)
            grant = await self._grant(conn, row)
            await self._raise_limit(conn, row, target, "owner_resume", grant)
            if "wall_mode" in proposal or "wall_seconds" in proposal:
                # Resume is an explicit owner wall extension, measured from DB now.
                from .models import budget
                wall = budget({"start_calls": min(target, 200), "wall_mode": proposal.get("wall_mode"), "wall_seconds": proposal.get("wall_seconds")}, minimum=1)
                request = {**row["request"], "budget": {**row["request"]["budget"], "wall_mode": wall["wall_mode"], "wall_seconds": wall["wall_seconds"]}}
                await conn.execute("UPDATE agent_runs SET request=$4::jsonb,deadline=CASE WHEN $5::bigint IS NULL THEN NULL ELSE clock_timestamp()+$5*interval '1 second' END WHERE lane=$1 AND owner=$2 AND id=$3", *keys, canonical(request), wall["wall_seconds"])
            if await conn.fetchval("SELECT deadline<=clock_timestamp() FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3", *keys):
                raise ExecutionError("wall_extension_required", 409)
            # Reset only the failure detector, preserving accepted work and lineage.
            await conn.execute("UPDATE agent_failure_episodes SET state=resume_state,resume_state=NULL WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='paused' AND resume_state IS NOT NULL", *keys)
            await conn.execute("UPDATE agent_runs SET status='recovering',reason=NULL,owner_flag=EXISTS(SELECT 1 FROM agent_flags WHERE lane=$1 AND owner=$2 AND run_id=$3 AND status='open'),loop_state=loop_state-'last_failure'-'failure_cycles' WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
            await self._event(conn, keys, "owner_resumed", {"effective_call_limit": target, "wall_override": proposal})
            return await self._remember(conn, auth, "runs.resume", args["request_key"], args, row["id"], {"run_id": row["id"]})
