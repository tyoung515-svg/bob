"""Durable failure episodes and owner flags; uses the common run/pool locks."""
from __future__ import annotations

import uuid

from .failure import classify, effect_state, fingerprint, retry_delay, AUDIT_FIELDS
from .models import ExecutionError, canonical, digest, integer, only, request_key, text


def item(raw):
    from .store import record, decoded
    value = record(raw)
    for key in ("last_failure", "audit_input", "audit", "audit_binding", "resolution", "effect_receipt", "context"):
        if value.get(key) is not None:
            value[key] = decoded(value[key])
    return value


class FailureStore:
    async def failure_for_step(self, row, step):
        async with self.transaction() as c:
            await self._fenced(c, row)
            raw = await c.fetchrow("SELECT e.* FROM agent_failure_episodes e JOIN agent_run_steps s ON (e.lane,e.owner,e.run_id,e.id)=(s.lane,s.owner,s.run_id,s.failure_episode_id) WHERE s.lane=$1 AND s.owner=$2 AND s.run_id=$3 AND s.ordinal=$4",
                                   row["lane"], row["owner"], row["id"], step["ordinal"])
            return item(raw) if raw else None

    async def _episode(self, c, keys, episode_id):
        raw = await c.fetchrow("SELECT * FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 FOR UPDATE", *keys, episode_id)
        if not raw:
            raise ExecutionError("failure_episode_not_found", 409)
        return item(raw)

    async def failure_wait(self, row, episode_id):
        async with self.transaction() as c:
            current = await self._fenced(c, row)
            await self._dispatchable(c, current)
            return float(await c.fetchval("SELECT greatest(0,extract(epoch FROM retry_at-clock_timestamp())) FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4",
                                    row["lane"], row["owner"], row["id"], episode_id) or 0)

    async def _reserve_failure(self, c, row, step, binding, episode_id, purpose):
        keys = (row["lane"], row["owner"], row["id"])
        if not episode_id:
            if step["failure_episode_id"] or purpose != "initial":
                raise ExecutionError("failure_episode_required", 409)
            return
        episode = await self._episode(c, keys, episode_id)
        if episode["state"] in {"paused", "resolved"} or step["failure_episode_id"] != episode_id:
            raise ExecutionError("failure_episode_paused", 409)
        expected = {"basic": ("backoff", "basic_retries", 2), "audit": ("audit", "audit_calls", 3), "guided": ("guided", "guided_calls", 1)}.get(purpose)
        if not expected or episode["state"] != expected[0] or episode[expected[1]] >= expected[2]:
            raise ExecutionError("recovery_attempt_exhausted", 409)
        if episode["effect_status"] not in {"none", "not_applied"}:
            raise ExecutionError("effect_reconciliation_required", 409)
        if effect_state(item(step)) not in {"none", "not_applied"}:
            raise ExecutionError("effect_reconciliation_required", 409)
        if episode["retry_at"] and await c.fetchval("SELECT $1::timestamptz>clock_timestamp()", episode["retry_at"]):
            raise ExecutionError("retry_not_due", 409)
        pin = episode["audit_binding"] if purpose == "audit" else episode["binding"]
        if binding != pin:
            raise ExecutionError("binding_changed", 409)

    async def _charge_failure(self, c, row, attempt):
        if not attempt["failure_episode_id"]:
            return
        field = {"basic": "basic_retries", "audit": "audit_calls", "guided": "guided_calls"}[attempt["purpose"]]
        await c.execute("UPDATE agent_failure_episodes SET " + field + "=" + field + "+1,updated_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4",
                        row["lane"], row["owner"], row["id"], attempt["failure_episode_id"])

    async def record_failure(self, row, step, attempt, binding, exc, *, episode_id=None, purpose="initial", returned=False, raw=None, usage=None):
        category, code = classify(exc)
        detail = exc.detail if isinstance(exc, ExecutionError) and isinstance(exc.detail, dict) else {}
        if not isinstance(exc, ExecutionError):
            status = getattr(exc, "status", None)
            headers = getattr(exc, "headers", None)
            detail = {"status": status if type(status) is int else None,
                      "retry_after": headers.get("Retry-After") if headers else None}
        # Store typed errors, not arbitrary exception strings/HTTP bodies/secrets.
        failure = {"class": category, "code": code, "status": detail.get("status"), "checks": detail.get("failures", [])}
        async with self.transaction() as c:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(c, keys)
            current = await self._fenced(c, row)
            await self._dispatchable(c, current)  # cancellation/revocation win
            stored_step = item(await c.fetchrow("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, step["ordinal"]))
            effect_class = stored_step["effect_class"]
            # Only server adapters may store these receipts. Model audit output
            # cannot reconcile effects, and an idle worker is not such a receipt.
            effect = effect_state(stored_step)
            if not episode_id:
                episode_id = str(uuid.uuid4())
                await c.execute("INSERT INTO agent_failure_episodes(lane,owner,run_id,id,fingerprint,ordinal,binding,state,effect_class,effect_status,last_failure) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,'backoff',$8,$9,$10::jsonb) ON CONFLICT(lane,owner,run_id,fingerprint) DO NOTHING",
                    *keys, episode_id, fingerprint(step, code), step["ordinal"], canonical(binding), effect_class, effect, canonical(failure))
                episode_id = await c.fetchval("SELECT id FROM agent_failure_episodes WHERE lane=$1 AND owner=$2 AND run_id=$3 AND fingerprint=$4", *keys, fingerprint(step, code))
            episode = await self._episode(c, keys, episode_id)
            if attempt:
                changed = await c.fetchval("UPDATE agent_attempts SET state='failed',failure_episode_id=$6,failure_detail=$7::jsonb,response=$8,response_usage=$9::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND generation=$5 AND state='dispatched' RETURNING id",
                    *keys, attempt, row["generation"], episode_id, canonical(failure), raw[:16000 if purpose == "audit" else 60000] if isinstance(raw, str) else None, canonical(usage or {}))
                if not changed:
                    raise ExecutionError("unsettled_attempts", 409)
            if purpose == "audit" and returned and isinstance(raw, str):
                await c.execute("UPDATE agent_failure_episodes SET raw_audit=$5 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode_id, raw[:16000])
            await c.execute("UPDATE agent_run_steps SET state='ready',failure_episode_id=$5 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, step["ordinal"], episode_id)
            reason = None
            if episode["state"] in {"resolved", "paused"}:
                reason = "failure_episode_exhausted"
            elif effect in {"unknown", "applied"}:
                reason = "effect_" + effect
            elif category == "forbidden":
                reason = code
            elif purpose == "guided":
                reason = "guided_retry_failed"
            elif purpose == "audit" and (returned or category != "transient" or episode["audit_calls"] >= 3):
                reason = "invalid_audit" if returned else "second_eye_unavailable"
            if not attempt:
                reason = code  # no reservation: never auto-grow the recovery budget
            state = "paused" if reason else ("audit" if purpose == "audit" or episode["basic_retries"] >= 2 else "backoff")
            delay = retry_delay(episode["audit_calls"] - 1 if purpose == "audit" else episode["basic_retries"], detail.get("retry_after"))
            if state == "audit" and purpose != "audit":
                delay = 0
            resume_state = episode["state"] if reason in {"call_budget_exhausted", "wall_time_expired"} and episode["state"] in {"backoff", "audit", "guided"} else None
            await c.execute("UPDATE agent_failure_episodes SET state=$5,last_failure=$6::jsonb,effect_status=$7,retry_at=CASE WHEN $9::text IS NOT NULL THEN $10::timestamptz ELSE clock_timestamp()+$8*interval '1 second' END,resume_state=$9,updated_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4",
                            *keys, episode_id, state, canonical(failure), effect, delay, resume_state, episode["retry_at"])
            await c.execute("UPDATE agent_runs SET phase=$4 WHERE lane=$1 AND owner=$2 AND id=$3", *keys, "retry_backoff" if state == "backoff" else state)
            await self._event(c, keys, "failure_recorded", {"episode_id": episode_id, "step": step["ordinal"], "attempt_id": attempt, "purpose": purpose, "failure": failure, "effect_status": effect, "next": state, "backoff_seconds": delay})
            if state == "audit":
                await self._audit_input(c, current, episode_id, step)
            if reason:
                await self._failure_pause(c, current, episode_id, step["ordinal"], reason)
            return await self._episode(c, keys, episode_id)

    async def _audit_input(self, c, row, episode_id, step):
        keys = (row["lane"], row["owner"], row["id"])
        episode = await self._episode(c, keys, episode_id)
        attempts = await c.fetch("SELECT id,purpose,binding,failure_detail,input_digest,state,response FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND failure_episode_id=$4 ORDER BY dispatched_at,id", *keys, episode_id)
        from .store import decoded
        trail = [{**dict(a), "binding": decoded(a["binding"]), "failure_detail": decoded(a["failure_detail"])} for a in attempts]
        for entry in trail:
            if entry["response"] is not None:
                entry["response_digest"] = digest(entry["response"])
                entry["response"] = entry["response"][:8000]
        remaining = await c.fetchval("SELECT call_limit-consumed FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        data = {"schema_version": 1, "failed_step_id": str(step["ordinal"]), "task": row["request"]["task"],
                "original_evidence_ids": row["request"].get("evidence_ids", []), "acceptance": row["request"].get("acceptance"),
                "binding": episode["binding"], "attempt_ids": [a["id"] for a in attempts], "trail": trail,
                "evidence_refs": list(dict.fromkeys(row["request"].get("evidence_ids", []) + ["attempt:" + a["id"] for a in attempts])),
                "check_ids": ["acceptance"], "checks": episode["last_failure"].get("checks", []),
                "effect_class": episode["effect_class"], "effect_status": episode["effect_status"],
                "authority": {"scope": row["run_authority"]["scope"], "selected_grant_id": row["selected_grant_id"]},
                "remaining_calls": remaining, "audit_schema_fields": sorted(AUDIT_FIELDS),
                "response_template": {"schema_version": 1, "failed_step_id": str(step["ordinal"]),
                    "attempt_ids": [a["id"] for a in attempts], "reviewer": {"binding_id": "server-stamped", "model_id": "server-stamped"},
                    "cause": "Explain the cause using the evidence refs", "evidence_refs": ["attempt:" + a["id"] for a in attempts],
                    "uncertainties": [], "effect_assessment": episode["effect_status"], "recommended_changes": ["Bounded correction"],
                    "required_checks": ["acceptance"], "recommendation": "guided_retry", "confidence": "medium"},
                "audit_contract": {"recommendation": ["guided_retry", "pause"], "confidence": ["low", "medium", "high"],
                                   "effect_assessment": ["none", "applied", "not_applied", "unknown"],
                                   "reviewer": {"binding_id": "server-stamped", "model_id": "server-stamped"}}}
        await c.execute("UPDATE agent_failure_episodes SET audit_input=$5::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode_id, canonical(data))

    async def set_auditor(self, row, episode_id, key, binding):
        async with self.transaction() as c:
            current = await self._fenced(c, row)
            await self._dispatchable(c, current)
            keys = (row["lane"], row["owner"], row["id"])
            episode = await self._episode(c, keys, episode_id)
            if episode["auditor"] and (episode["auditor"] != key or episode["audit_binding"] != binding):
                raise ExecutionError("binding_changed", 409)
            await c.execute("UPDATE agent_failure_episodes SET auditor=$5,audit_binding=$6::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode_id, key, canonical(binding))
            step = item(await c.fetchrow("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, episode["ordinal"]))
            await self._audit_input(c, current, episode_id, step)
            return await self._episode(c, keys, episode_id)

    async def audit_result(self, row, episode_id, attempt, raw, parsed, usage):
        async with self.transaction() as c:
            keys = (row["lane"], row["owner"], row["id"])
            await self._lock_pools(c, keys)
            current = await self._fenced(c, row)
            await self._dispatchable(c, current)
            episode = await self._episode(c, keys, episode_id)
            changed = await c.fetchval("UPDATE agent_attempts SET state='completed',response=$6,response_usage=$7::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND generation=$5 AND state='dispatched' AND purpose='audit' RETURNING id", *keys, attempt, row["generation"], raw, canonical(usage))
            if not changed:
                raise ExecutionError("stale_attempt", 409)
            allowed = parsed["recommendation"] == "guided_retry" and parsed["effect_assessment"] in {"none", "not_applied"} and episode["effect_status"] in {"none", "not_applied"} and episode["guided_calls"] == 0
            await c.execute("UPDATE agent_failure_episodes SET audit=$5::jsonb,raw_audit=$6,state=$7,retry_at=NULL WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode_id, canonical(parsed), raw, "guided" if allowed else "paused")
            await c.execute("UPDATE agent_run_steps SET state='ready' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, episode["ordinal"])
            await self._event(c, keys, "second_eye_audit", {"episode_id": episode_id, "attempt_id": attempt, "reviewer": parsed["reviewer"], "recommendation": parsed["recommendation"], "guided_retry_allowed": allowed,
                                                         "audit": parsed, "raw_digest": digest(raw)})
            if not allowed:
                await self._failure_pause(c, current, episode_id, episode["ordinal"], "audit_requested_pause")
            return await self._episode(c, keys, episode_id)

    async def _finish_failure(self, c, row, ordinal, attempt, output):
        keys = (row["lane"], row["owner"], row["id"])
        a = await c.fetchrow("SELECT failure_episode_id,purpose FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, attempt)
        if not a or not a["failure_episode_id"]:
            return
        from .failure import validate_output
        step = item(await c.fetchrow("SELECT * FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, ordinal))
        checks = validate_output(row, step, output)
        await c.execute("UPDATE agent_failure_episodes SET state='resolved',retry_at=NULL,updated_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, a["failure_episode_id"])
        await self._event(c, keys, "failure_recovered", {"episode_id": a["failure_episode_id"], "attempt_id": attempt, "purpose": a["purpose"], "required_checks": ["acceptance"], "checks": checks, "verification": "server_deterministic_checks"})

    async def _failure_pause(self, c, row, episode_id, ordinal, reason):
        keys = (row["lane"], row["owner"], row["id"])
        await c.execute("UPDATE agent_failure_episodes SET resume_state=CASE WHEN $5 IN ('call_budget_exhausted','wall_time_expired') AND state IN ('backoff','audit','guided') THEN state ELSE resume_state END,state='paused',updated_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4", *keys, episode_id, reason)
        await self._flag(c, row, reason, episode_id=episode_id, ordinal=ordinal)
        await self._pause(c, row, reason, owner_flag=True)

    async def _flag(self, c, row, cause, *, episode_id=None, ordinal=None):
        keys = (row["lane"], row["owner"], row["id"])
        key = episode_id or digest([row["id"], cause])
        flag_id = str(uuid.uuid4())
        account = await c.fetchrow("SELECT call_limit,consumed,unknown_calls FROM agent_budget_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        reserved = await c.fetchval("SELECT reserved FROM agent_budget_allocations WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id='root'", *keys)
        # Worker authority does not require a model-call account/allocation.
        # Flagging must not roll back revocation or policy narrowing for such a
        # run, nor manufacture call authority to make the snapshot possible.
        # Read job holds without locking/mutating them: the queue owns settlement
        # after cleanup, and callers may already hold the run/pool row locks.
        jobs = await c.fetchrow("SELECT * FROM run_job_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3", *keys)
        context = {"initial_budget": row["initial_budget"], "request": row["request"], "account": dict(account) if account is not None else None, "reserved_calls": reserved,
                   "job_account": {k: v for k, v in dict(jobs).items() if k not in {"lane", "owner", "run_id"}} if jobs is not None else None,
                   "selected_grant_id": row["selected_grant_id"], "selected_grant_revision": row["selected_grant_revision"]}
        created = await c.fetchval("INSERT INTO agent_flags(lane,owner,run_id,id,episode_key,episode_id,ordinal,cause,phase,context) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb) ON CONFLICT(lane,owner,run_id,episode_key) DO NOTHING RETURNING id", *keys, flag_id, key, episode_id, ordinal, cause, row["phase"], canonical(context))
        if created:
            await self._event(c, keys, "flag_raised", {"flag_id": created, "episode_id": episode_id, "cause": cause})
        await c.execute("UPDATE agent_runs SET owner_flag=EXISTS(SELECT 1 FROM agent_flags WHERE lane=$1 AND owner=$2 AND run_id=$3 AND status='open') WHERE lane=$1 AND owner=$2 AND id=$3", *keys)

    async def _flag_row(self, c, auth, flag_id):
        raw = await c.fetchrow("SELECT f.* FROM agent_flags f JOIN agent_runs r ON (f.lane,f.owner,f.run_id)=(r.lane,r.owner,r.id) WHERE f.lane=$1 AND f.owner=$2 AND f.id=$3 AND ($4::text IS NULL OR r.boundary=$4)", auth.lane, auth.owner, flag_id, auth.boundary if auth.token_type == "agent" else None)
        if not raw:
            raise ExecutionError("flag_not_found", 404)
        return item(raw)

    async def flags(self, auth, args):
        only(args, {"run_id", "status", "cursor", "limit"})
        limit = integer(args.get("limit", 20), 1, 50)
        if args.get("status", "open") not in {"open", "resolved", "all"}:
            raise ExecutionError("invalid_flag_status")
        # Agent enumeration is explicitly restricted to a currently visible run.
        if auth.token_type == "agent" and not args.get("run_id"):
            raise ExecutionError("run_id_required", 403)
        async with self.transaction() as c:
            if args.get("run_id"):
                await self._row(c, auth, args["run_id"])
            rows = await c.fetch("SELECT f.* FROM agent_flags f JOIN agent_runs r ON (f.lane,f.owner,f.run_id)=(r.lane,r.owner,r.id) WHERE f.lane=$1 AND f.owner=$2 AND f.id>$3 AND ($4::text IS NULL OR f.run_id=$4) AND ($5='all' OR f.status=$5) AND ($6::text IS NULL OR r.boundary=$6) ORDER BY f.id LIMIT $7",
                                 auth.lane, auth.owner, args.get("cursor", ""), args.get("run_id"), args.get("status", "open"), auth.boundary if auth.token_type == "agent" else None, limit+1)
            return {"items": [self._flag_summary(item(r)) for r in rows[:limit]], "next_cursor": rows[limit-1]["id"] if len(rows)>limit else None, "source": "server"}

    def _flag_summary(self, flag):
        return {k: (flag[k].isoformat() if k in {"created_at", "updated_at"} else flag[k]) for k in
                ("id", "run_id", "episode_id", "ordinal", "cause", "phase", "revision", "status", "created_at", "updated_at", "resolution")}

    async def flag_get(self, auth, args):
        only(args, {"flag_id", "after_event_seq", "job_cursor"})
        after = integer(args.get("after_event_seq", 0), 0)
        job_cursor = text(args["job_cursor"], 128) if args.get("job_cursor") else ""
        async with self.transaction() as c:
            flag = await self._flag_row(c, auth, args.get("flag_id"))
            row = await self._row(c, auth, flag["run_id"], lock=True)
            view = await self._public(c, row, after)
            keys = (auth.lane, auth.owner, row["id"])
            episode = await self._episode(c, keys, flag["episode_id"]) if flag["episode_id"] else None
            if episode:
                for k in ("created_at", "updated_at", "retry_at"):
                    episode[k] = episode[k].isoformat() if episode[k] else None
                for k in ("lane", "owner"):
                    episode.pop(k, None)
            trail = [item(a) for a in await c.fetch("SELECT id,ordinal,purpose,state,binding,input_digest,failure_detail,response,response_usage FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND failure_episode_id=$4 ORDER BY dispatched_at,id", *keys, flag["episode_id"])] if episode else []
            from .store import decoded
            for a in trail:
                for key in ("failure_detail", "response_usage"):
                    a[key] = decoded(a[key])
            # Inspect even revoked/expired authority; reading a flag must never
            # select a new grant or fail because the original grant is revoked.
            grant = await c.fetchrow("SELECT * FROM agent_execution_grants WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, row["selected_grant_id"]) if row["selected_grant_id"] else None
            jobs = [dict(j) for j in await c.fetch("SELECT execution_id,outcome,cleanup,reconciliation,cancel_requested FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND execution_id>$4 ORDER BY execution_id LIMIT 51", *keys, job_cursor)]
            run = view["run"]
            return {"flag": {**self._flag_summary(flag), "project_id": run.get("project_id"), "conversation_id": run.get("conversation_id"),
                    "scope": row["run_authority"]["scope"], "policy": {"team_revision": row["team_revision"], "team_snapshot": row["snapshot"]["team"],
                        "bindings": row["snapshot"]["models"], "selected_grant_id": row["selected_grant_id"],
                        "authorized_call_ceiling": grant["call_ceiling"] if grant else 200},
                    "budget": {k: run[k] for k in ("initial_budget", "requested_start_calls", "effective_call_limit", "calls_used", "unknown_calls", "reserved_calls", "pool_holds", "deadline", "job_account")},
                    "stopped_context": flag["context"],
                    "usage_estimates": {"billing_accurate": False, "source": "attempt_and_step_receipts"},
                    "effects": episode["effect_status"] if episode else ("unknown" if "unknown" in flag["cause"] else "inspect_receipts"),
                    "cleanup_pending": row["cleanup_pending"], "cleanup_source": "worker_job_receipts" if flag["cause"] == "worker_outcome_unknown" else "run_transport",
                    "live_job_cleanup": jobs[:50],
                    "safe_next_actions": ["inspect", "resume with guidance", "authorize budget", "cancel"],
                    "action_contract": {"resume with guidance": "records_owner_decision_only; runs.resume_revalidates_authority_and_never_resets_recovery_counts",
                                        "authorize budget": "separate_owner_grant_required", "cancel": "runs.cancel"},
                    "episode": episode, "attempts": trail, "output_links": run["accepted_outputs"],
                    "trail_ref": {"operation": "runs.get", "run_id": row["id"], "after_event_seq": after},
                    "resolution_does_not_resume": True}, "events": view["events"], "next_cursor": view["next_cursor"],
                    "next_job_cursor": jobs[49]["execution_id"] if len(jobs)>50 else None,
                    "run": {k: run[k] for k in ("id", "project_id", "conversation_id", "selected_grant_id", "status", "revision")}}

    async def flag_resolve(self, auth, args):
        only(args, {"flag_id", "expected_revision", "request_key", "decision", "guidance"})
        if auth.token_type != "human" or auth.actor != auth.owner:
            raise ExecutionError("not_found", 404)
        request_key(args.get("request_key")); integer(args.get("expected_revision"))
        if args.get("decision") not in {"inspected", "resume_with_guidance", "authorize_budget", "cancel"}:
            raise ExecutionError("invalid_flag_decision")
        guidance = text(args.get("guidance"), 4000)
        async with self.transaction() as c:
            flag = await self._flag_row(c, auth, args.get("flag_id"))
            row = await self._row(c, auth, flag["run_id"], lock=True)
            flag = await self._flag_row(c, auth, args.get("flag_id"))
            prior = await self._mutation(c, auth, "flags.resolve", args["request_key"], args)
            if prior:
                return prior["result"]
            if flag["revision"] != args["expected_revision"] or flag["status"] != "open":
                raise ExecutionError("revision_conflict", 409)
            resolution = {"decision": args["decision"], "guidance": guidance, "actor": auth.actor, "credential_epoch": auth.credential_epoch}
            await c.execute("UPDATE agent_flags SET status='resolved',revision=revision+1,resolution=$4::jsonb,updated_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND id=$3", auth.lane, auth.owner, flag["id"], canonical(resolution))
            keys = (auth.lane, auth.owner, flag["run_id"])
            await c.execute("UPDATE agent_runs SET owner_flag=EXISTS(SELECT 1 FROM agent_flags WHERE lane=$1 AND owner=$2 AND run_id=$3 AND status='open') WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
            await self._event(c, keys, "flag_resolved", {"flag_id": flag["id"], **resolution})
            return await self._remember(c, auth, "flags.resolve", args["request_key"], args, flag["id"], {"flag_id": flag["id"], "run_id": flag["run_id"], "resolved": True, "run_resumed": False})
