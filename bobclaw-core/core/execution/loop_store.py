"""Durable generic text topology atop the common run/attempt ledger."""
from .models import ExecutionError, canonical, digest
from .planning import check_output, parse_plan


class LoopStore:
    async def _complete_loop(self, conn, row):
        from .store import decoded
        keys = (row["lane"], row["owner"], row["id"])
        outputs = await conn.fetch("SELECT criteria_ids FROM agent_accepted_outputs WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        satisfied = {c for o in outputs for c in decoded(o["criteria_ids"])}
        if not all(c["id"] in satisfied for c in row["request"]["acceptance"]["criteria"] if c.get("required",True)):
            return False
        if await conn.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state IN ('reserved','dispatched','unknown')", *keys):
            return False
        # Backoff and the gaps between audit/guided calls have no unsettled
        # attempt, but still belong to the active wave. Keep their controller.
        if await conn.fetchval("SELECT count(*) FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state<>'resolved'", *keys):
            return False
        if row['request'].get('worker_contract'):
            if row.get('cleanup_pending'):
                return False
            if await conn.fetchval("SELECT count(*) FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (outcome NOT IN ('succeeded','failed','timed_out','cancelled') OR cleanup<>'idle_proved' OR reconciliation<>'established')", *keys):
                return False
            if await conn.fetchval("SELECT count(*) FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (state<>'completed' OR checks->>'passed' IS DISTINCT FROM 'true')", *keys):
                return False
        await self._release_holds(conn, keys)
        await conn.execute("UPDATE agent_runs SET status='completed',phase='integrate',reason=NULL,controller=NULL,lease_until=NULL WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
        await self._event(conn, keys, "completed", {"required_criteria": sorted(satisfied)})
        return True

    async def fail_loop_attempt(self, row, attempt):
        async with self.transaction() as conn:
            await self._fenced(conn, row)
            await conn.execute("UPDATE agent_attempts SET state='failed' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND generation=$5 AND state='dispatched'",
                               row["lane"], row["owner"], row["id"], attempt, row["generation"])

    async def loop_dependencies(self, row, step):
        from .store import record
        async with self.transaction() as conn:
            return [record(s) for s in await conn.fetch(
                "SELECT output,output_digest,checks FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 "
                "AND (spec->>'cycle')::bigint=$4 AND spec->>'id'=ANY($5::text[]) AND state='completed' ORDER BY ordinal",
                row["lane"], row["owner"], row["id"], step["spec"]["cycle"], step["spec"]["dependencies"])]

    async def loop_view(self, row):
        from .store import record
        async with self.transaction() as conn:
            current = await self._fenced(conn, row)
            keys = (row["lane"], row["owner"], row["id"])
            outputs = [record(o) for o in await conn.fetch("SELECT * FROM agent_accepted_outputs WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY ordinal", *keys)]
            receipts = [record(e) for e in await conn.fetch("SELECT kind,detail FROM agent_run_events WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY seq DESC LIMIT 8", *keys)]
            return {"row": current, "outputs": outputs, "recent_receipts": list(reversed(receipts))}

    async def loop_next(self, row):
        from .store import record
        async with self.transaction() as conn:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(conn, keys)
            current = await self._fenced(conn, row)
            await self._dispatchable(conn, current)
            if await self._complete_loop(conn, current):
                return {"action": "completed"}
            accepted = await conn.fetch("SELECT criteria_ids FROM agent_accepted_outputs WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            state = current["loop_state"]
            steps = [record(s) for s in await conn.fetch("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (spec->>'cycle')::bigint=$4 ORDER BY ordinal", *keys, state.get("cycle", 0))]
            by_id = {s["spec"].get("id"): s for s in steps if s["spec"].get("phase") == "task"}
            ready = []
            for step in steps:
                if step["state"] != "ready":
                    continue
                if all(by_id[d]["state"] == "completed" and by_id[d]["checks"]["passed"] for d in step["spec"].get("dependencies", [])):
                    ready.append(step)
            if ready:
                if current['request'].get('worker_contract'):
                    # Typed work is sequential within a run; the lane queue still
                    # provides fairness across runs and ordinary tools. Executable
                    # steps do not consume or require a model-call credit.
                    worker = next((s for s in ready if s['spec'].get('kind') not in (None, 'model_task')), None)
                    if worker:
                        return {'action': 'steps', 'steps': [worker]}
                free = await conn.fetchval("SELECT allowance-consumed-reserved-delegated FROM agent_budget_allocations WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id='root'", *keys)
                if free < 1:
                    recovery = next((s for s in ready if s["failure_episode_id"]), None)
                    if recovery:
                        await self._failure_pause(conn, current, recovery["failure_episode_id"], recovery["ordinal"], "call_budget_exhausted")
                        return {"action": "paused"}
                    return {"action": "budget", "revision": current["revision"]}
                parallelism = 1 if current['request'].get('worker_contract') else min(2, current["snapshot"]["team"].get("max_parallelism", 2))
                return {"action": "steps", "steps": ready[:min(parallelism, free)]}
            if any(s["state"] in {"reserved", "dispatched"} for s in steps):
                return {"action": "settling"}
            if steps and not state.get("closed", False):
                # No ready work remains: failed dependencies cannot be dispatched.
                failures = sorted({failure for s in steps for failure in (s["checks"] or {}).get("failures", [])})
                fingerprint = digest(failures if failures else [s["spec"].get("instruction") for s in steps])
                progress = len(accepted) > state.get("accepted_before", 0)
                cycles = 0 if progress else (state.get("failure_cycles", 0) + 1 if state.get("last_failure") == fingerprint else 1)
                state = {**state, "closed": True, "last_failure": fingerprint, "failure_cycles": cycles}
                await conn.execute("UPDATE agent_runs SET loop_state=$4::jsonb WHERE lane=$1 AND owner=$2 AND id=$3", *keys, canonical(state))
                await self._event(conn, keys, "cycle_checked", {"cycle": state["cycle"], "failures": failures, "equivalent_cycles": cycles})
                if cycles >= 3:
                    await self._pause(conn, current, "no_progress", owner_flag=True)
                    return {"action": "paused"}
            account = await conn.fetchrow("SELECT * FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            reserved = await conn.fetchval("SELECT reserved FROM agent_budget_allocations WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id='root'", *keys)
            if account["consumed"] + reserved >= account["call_limit"]:
                # Revision includes the cycle-close event above.
                revision = await conn.fetchval("SELECT revision FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
                return {"action": "budget", "revision": revision}
            ordinal = await conn.fetchval("SELECT COALESCE(max(ordinal),-1)+1 FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
            if row.get('checked_context', {}).get('direct_mode'):
                from .checked import direct_plan
                plan = direct_plan(current['request']['acceptance'], row['checked_context'])
                cycle = state.get('cycle', 0) + 1
                for offset, task in enumerate(plan['tasks']):
                    task_spec = {**task,'phase':'task','cycle':cycle,'plan_step':ordinal,
                                 'criteria_ids':task.get('criteria_ids',[]),
                                 'effect_class':('read_only' if task['kind']=='model_task' else
                                                 'idempotent_by_key' if task['kind']=='proposal_publish' else 'workspace_mutation'),
                                 'operation_key':digest([row['id'],ordinal+offset])}
                    await conn.execute("INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,spec) VALUES ($1,$2,$3,$4,'worker',$5::jsonb)",*keys,ordinal+offset,canonical(task_spec))
                state={**state,'cycle':cycle,'closed':False,'accepted_before':len(accepted)}
                await conn.execute("UPDATE agent_runs SET loop_state=$4::jsonb,phase='task' WHERE lane=$1 AND owner=$2 AND id=$3",*keys,canonical(state))
                await self._event(conn,keys,'installed_recipe_ready',{'cycle':cycle,'mode':row['checked_context']['direct_mode'],'model_calls':0})
                first=record(await conn.fetchrow('SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4',*keys,ordinal))
                return {'action':'steps','steps':[first]}
            spec = {"phase": "plan", "cycle": state.get("cycle", 0) + 1}
            await conn.execute("INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,spec) VALUES ($1,$2,$3,$4,'manager',$5::jsonb)", *keys, ordinal, canonical(spec))
            state = {**state, "cycle": spec["cycle"], "closed": False, "accepted_before": len(accepted)}
            await conn.execute("UPDATE agent_runs SET loop_state=$4::jsonb,phase='plan' WHERE lane=$1 AND owner=$2 AND id=$3", *keys, canonical(state))
            await self._event(conn, keys, "plan_ready", {"step": ordinal, "cycle": spec["cycle"]})
            step = record(await conn.fetchrow("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, ordinal))
            return {"action": "steps", "steps": [step]}

    async def checkpoint_loop(self, row, step, attempt, output, usage):
        # Compute on bounded text before taking locks; publish checks and output in
        # the same transaction as attempt settlement. No model integrator needed.
        contract = row["request"]["acceptance"]
        spec = step["spec"]
        plan = None
        if spec["phase"] == "plan":
            try:
                if row['request'].get('worker_contract'):
                    from .checked import parse_plan as checked_plan
                    plan = checked_plan(output, contract, row['checked_context'])
                else:
                    plan = parse_plan(output, contract)
                checks = {"passed": True, "checks": ["plan_schema", "acyclic", "depth", "wave", "output_kinds"], "failures": []}
            except ExecutionError as exc:
                checks = {"passed": False, "failures": [exc.code]}
        elif row['request'].get('worker_contract'):
            from .checked import edits
            from .planning import strict_json
            value = edits(strict_json(output))
            checks = {'passed': True, 'checks': ['edit_schema'], 'failures': [], 'criteria_ids': []}
            async with self.transaction() as c:
                from .checked_work import source, source_workspace
                sha = await source(c, row, step)
                workspace_id = await source_workspace(c, row, step)
            output = canonical({**value, 'snapshot_digest': sha, 'workspace_id': workspace_id})
        else:
            checks = check_output(output, spec, contract, row["request"].get("evidence_ids", []))
        async with self.transaction() as conn:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(conn, keys)
            current = await self._fenced(conn, row)
            await self._dispatchable(conn, current)
            changed = await conn.fetchval("UPDATE agent_attempts SET state='completed' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND generation=$5 AND state='dispatched' RETURNING id", *keys, attempt, row["generation"])
            if not changed:
                raise ExecutionError("stale_attempt", 409)
            await self._finish_failure(conn, current, step["ordinal"], attempt, output)
            receipt = {k: v for k, v in checks.items() if k != "content"}
            await conn.execute("UPDATE agent_run_steps SET state='completed',output=$5,output_digest=$6,usage=$7::jsonb,checks=$8::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, step["ordinal"], output, digest(output), canonical(usage), canonical(receipt))
            await conn.execute("UPDATE agent_runs SET phase='check' WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
            await self._event(conn, keys, "step_checked", {"step": step["ordinal"], "attempt_id": attempt, "output_digest": digest(output), "checks": {k: v for k, v in checks.items() if k != "content"}})
            if plan:
                ordinal = await conn.fetchval("SELECT max(ordinal)+1 FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
                for offset, task in enumerate(plan["tasks"]):
                    task_spec = {**task, "phase": "task", "cycle": spec["cycle"], "plan_step": step["ordinal"]}
                    if row['request'].get('worker_contract'):
                        task_spec.setdefault('criteria_ids', [])
                        task_spec['effect_class'] = ('read_only' if task['kind'] in {'model_task','workspace_read'} else
                                                     'idempotent_by_key' if task['kind']=='proposal_publish' else 'workspace_mutation')
                        task_spec['operation_key'] = digest([row['id'],ordinal+offset])
                    await conn.execute("INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,spec) VALUES ($1,$2,$3,$4,'worker',$5::jsonb)", *keys, ordinal + offset, canonical(task_spec))
            elif checks["passed"] and spec["criteria_ids"]:
                # An output must satisfy at least one declared deliverable check.
                # Duplicate criteria do not accumulate unlimited context/outputs.
                prior = await conn.fetch("SELECT criteria_ids FROM agent_accepted_outputs WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
                from .store import decoded
                satisfied = {cid for o in prior for cid in decoded(o["criteria_ids"])}
                if set(spec["criteria_ids"]) - satisfied:
                    output_id = "output-" + str(step["ordinal"])
                    receipt = {**receipt, "run_id": row["id"], "step_lineage": [spec["plan_step"], step["ordinal"]]}
                    await conn.execute("INSERT INTO agent_accepted_outputs VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb)", *keys, output_id, step["ordinal"], digest(checks["content"]), checks["content"], canonical(spec["criteria_ids"]), canonical(receipt))
                    await self._event(conn, keys, "output_accepted", {"output_id": output_id, "digest": digest(checks["content"]), "step_lineage": [spec["plan_step"], step["ordinal"]], "check_receipts": receipt})
                    await conn.execute("UPDATE agent_runs SET phase='integrate' WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
            return "completed" if await self._complete_loop(conn, current) else "checkpointed"
