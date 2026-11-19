"""Supervised core pump, independent of HTTP/gateway lifetimes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
import uuid

from .models import LOOP_TOPOLOGY, ExecutionError, canonical, text

log = logging.getLogger(__name__)


def _log_unexpected(row, step, attempt, exc):
    if isinstance(exc, ExecutionError):
        return
    # Preserve frames, but never include arbitrary provider/model exception text
    # (including KeyError keys) or chained exceptions in the journal.
    safe = RuntimeError(type(exc).__name__)
    log.exception("Unexpected execution failure run=%s step=%s attempt=%s",
                  row.get("id"), (step or {}).get("ordinal"),
                  attempt.get("id") if isinstance(attempt, dict) else attempt,
                  exc_info=(type(safe), safe, exc.__traceback__))


def prompt_for(row, steps, ordinal):
    request = row["request"]
    evidence = "ORIGINAL REQUEST / SUPPLIED EVIDENCE:\n" + request["task"] + row.get("job_evidence", "")
    evidence += "\nSOURCE IDS: " + canonical(request.get("evidence_ids", []))
    evidence += "\nACCEPTANCE: " + (request.get("acceptance") or "Answer the bounded supplied-evidence task.")
    if ordinal == 0:
        return 'Plan ONE read-only worker task. Return only JSON {"tasks":["self-contained instruction"]}.\n' + evidence
    plan = steps[0]["output"]
    try:
        tasks = json.loads(plan.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())["tasks"]
        if not isinstance(tasks, list) or len(tasks) != 1:
            raise ValueError()
        task = text(tasks[0], 30000)
    except (ValueError, KeyError, TypeError, ExecutionError):
        raise ExecutionError("invalid_manager_plan", 502)
    if ordinal == 1:
        return "Execute this supplied-evidence reasoning task. No tools or execution claims.\n" + task + "\n" + evidence
    if ordinal == 2:
        return "Critique the worker's answer using the supplied evidence. A model review is not independent verification.\n" + evidence + "\nWORKER:\n" + steps[1]["output"]
    return "Integrate the worker and critic. Preserve sources, caveats and uncertainty; do not claim independent verification.\n" + evidence + "\nWORKER:\n" + steps[1]["output"] + "\nCRITIC:\n" + steps[2]["output"]


class Executor:
    def __init__(self, store, admission, dispatch, *, clock=None, monotonic=None, after_checkpoint=None, poll=0.25, audit_health=None):
        self.store, self.admission, self.dispatch = store, admission, dispatch
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.after_checkpoint, self.poll = after_checkpoint, poll
        self.audit_health = audit_health or (getattr(dispatch, "healthy") if callable(getattr(type(dispatch), "healthy", None)) else None)
        self.controller = str(uuid.uuid4())
        self.active = {}
        self.task = None
        from .workflow_controller import WorkflowController
        self.workflow = WorkflowController(self)
        self.store.workflow_controller = self.workflow

    def start(self):
        try:
            from core.nodes.execute import _run_tool_loop
            self.workflow.running = callable(_run_tool_loop)
        except Exception:
            self.workflow.running = False
            log.warning('Workflow turn adapter unavailable; legacy execution remains enabled')
        self.task = asyncio.create_task(self.pump(), name="durable-execution-pump")

    async def close(self):
        self.workflow.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        pending = list(self.active.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def pump(self):
        while True:
            self.active = {k: t for k, t in self.active.items() if not t.done()}
            if sum(k not in self.workflow.parked for k in self.active) < self.workflow.capacity:
                try:
                    row = await self.store.claim(self.controller, workflows=self.workflow.running)
                    if row:
                        self.active[row["id"]] = asyncio.create_task(self.execute(row), name="run-" + row["id"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Execution storage unavailable; dispatch disabled", exc_info=True)
            await asyncio.sleep(self.poll)

    async def _call(self, row, binding, prompt):
        # Lifetime independent of HTTP. DB heartbeat also observes cancellation.
        remaining = 180
        started = self.monotonic()
        if row["deadline"]:
            remaining = min(remaining, max(0, (row["deadline"] - self.clock()).total_seconds()))
        from .jobs import JobContext, job_context
        with job_context(JobContext(row.get('owner', 'lane-service'), row.get('owner', 'lane-service'),
                                   'run:' + row.get('id', 'unknown'), row.get('boundary', 'internal'),
                                   row.get('id'), row.get('generation'))):
            task = asyncio.create_task(self.dispatch(binding, prompt))
        try:
            async with asyncio.timeout(remaining):
                while True:
                    done, _ = await asyncio.wait({task}, timeout=self.poll)
                    state = await self.store.heartbeat(row)
                    if state != "running":
                        raise ExecutionError("wall_time_expired" if state == "deadline" else state, 409)
                    if done:
                        return task.result()
                    if self.monotonic() - started >= remaining:
                        raise TimeoutError()
        except TimeoutError:
            expired = row["deadline"] and self.clock() >= row["deadline"]
            raise ExecutionError("wall_time_expired" if expired else "provider_timeout", 409) from None
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def execute(self, row):
        if row['snapshot']['team']['topology'] == 'workflow/v1':
            return await self.workflow.tick(row)
        if row["snapshot"]["team"]["topology"] == LOOP_TOPOLOGY:
            return await self.execute_loop(row)
        from .call_guard import required
        token = required.set(True)
        response_received = False
        step = attempt = None
        try:
            from .failure import FailureRecovery
            recovery = FailureRecovery(self)
            steps = await self.store.steps(row)
            from core.tool_jobs.service import evidence
            row["job_evidence"] = await evidence(self.store, row)
            for ordinal, step in enumerate(steps):
                if step["state"] == "completed":
                    continue
                attempt = None
                await self.admission.revalidate(row)
                prompt = prompt_for(row, steps, ordinal)
                binding_id = row["snapshot"]["team"]["roles"][step["role"]]
                binding = row["snapshot"]["models"][binding_id]
                response_received = False
                recovered = await recovery.call(row, step, binding, prompt)
                if recovered is None:
                    return
                attempt, output, usage, episode, checks = recovered
                response_received = True
                if not await self.store.checkpoint(row, ordinal, attempt, output, usage):
                    raise ExecutionError("run_stopped", 409)
                steps[ordinal] = {**step, "state": "completed", "output": output}
                if self.after_checkpoint:
                    await self.after_checkpoint(row, ordinal)
            from core.tool_jobs.service import finalize
            await finalize(self, row, steps)
        except asyncio.CancelledError:
            # Simulates orderly core shutdown. Uncertain dispatches reconcile at lease reclaim.
            try:
                await self.store.release(row)
            except Exception:
                pass  # expired lease/new generation/DB outage; recovery owns reconciliation
            raise
        except Exception as exc:
            _log_unexpected(row, step, attempt, exc)
            try:
                settle = getattr(self.store, "settle_revocation", None)
                if settle and await settle(row):
                    return
                # An explicit HTTP refusal/invalid returned answer is a charged
                # failed call, not an unknown transport outcome.
                known = response_received or (not isinstance(exc, ExecutionError) and isinstance(getattr(exc, "status", None), int))
                if isinstance(exc, ExecutionError) and exc.code in {"provider_error", "unexpected_tool_call", "provider_output_too_large", "invalid_provider_response"}:
                    known = True
                await self.store.stop(row, exc.code if isinstance(exc, ExecutionError) else type(exc).__name__, unknown=not known)
            except Exception:
                log.warning("Run %s awaits durable reconciliation", row["id"], exc_info=True)
        finally:
            required.reset(token)

    async def execute_loop(self, row):
        from .call_guard import required
        from .recovery import context_for
        token = required.set(True)
        step = None
        try:
            from .failure import FailureRecovery
            recovery = FailureRecovery(self)
            if row['request'].get('worker_contract'):
                from .checked_work import load_context
                row['checked_context'] = await load_context(self.store, row)
            while True:
                step = None
                await self.admission.revalidate(row)
                next_work = await self.store.loop_next(row)
                if next_work["action"] in {"completed", "paused"}:
                    return
                if next_work["action"] == "budget":
                    if await self.store.evaluate_growth(row, next_work["revision"]) == "grown":
                        continue
                    return
                if next_work["action"] == "settling":
                    raise ExecutionError("unsettled_attempts", 409)
                view = await self.store.loop_view(row)
                row["deadline"] = view["row"]["deadline"]
                context = context_for(row, view["outputs"], view["recent_receipts"])

                prepared = []
                for step in next_work["steps"]:
                    await self.admission.revalidate(row)
                    if row['request'].get('worker_contract') and step['spec'].get('kind') not in (None, 'model_task'):
                        from .checked_work import execute_step
                        checkpoint = await execute_step(self, row, step)
                        if self.after_checkpoint:
                            await self.after_checkpoint(row, step['ordinal'])
                        if checkpoint in {'completed', 'paused'}:
                            return
                        break
                    binding_id = row["snapshot"]["team"]["roles"][step["role"]]
                    binding = row["snapshot"]["models"][binding_id]
                    if row['request'].get('worker_contract'):
                        if step['spec']['phase'] == 'plan':
                            prompt = ('Return ONLY JSON {"tasks":[typed steps]}. Every step has id, kind, instruction, dependencies, snapshot, criteria_ids. '
                                      'snapshot is the base_digest or the ID of a direct dependency supplying a frozen snapshot. '
                                      'Kinds: model_task (operation_id=edits.plan), workspace_read (operation_id=workspace.read, paths), '
                                      'workspace_edit (operation_id=workspace.edit, edits_from=model task ID or inline edits), '
                                      'registered_check (check_id), worker_command (registered operation_id), proposal_publish. '
                                      'Executable steps require proposed_seconds 900/1800/3600 and rationale. '
                                      'Only proposal_publish assigns acceptance criteria; include all required checks and publish exactly once. '
                                      'Commands cannot create profiles, alter checks, or apply. Depth <=16, ready wave <=16. '
                                      'Read -> model_task -> edit -> registered checks on edited snapshot -> proposal_publish. '
                                      'Unchanged checks do not cover edited bytes.\nFROZEN REGISTRATION: '
                                      + canonical(row['checked_context']) + '\n' + context)
                        else:
                            from .checked_work import model_prompt
                            prompt = await model_prompt(self.store, row, step)
                    elif step["spec"]["phase"] == "plan":
                        prompt = ('Return ONLY JSON {"tasks":[{"id":"t1","instruction":"...",'
                                  '"dependencies":[],"output_kind":"answer","criteria_ids":["criterion-id"],"checks":{"min_length":1}}]}. '
                                  'Plan supplied-evidence text work only. Known output kinds: answer, report, proposal, json, intermediate. '
                                  'Depth <=4; ready wave <=16; no tools, jobs, files or capabilities. '
                                  'Acceptance criteria checks are fixed server-side; optional task checks can only add constraints. '
                                  'Checks allow min_length, max_length, required_sections, require_citations, json_type. '
                                  'Plans are not deliverables.\n' + context)
                    else:
                        dependencies = await self.store.loop_dependencies(row, step)
                        prompt = ('Return ONLY JSON {"content":"deliverable text","citations":["supplied-ID"]}. '
                                  'No tools or execution claims. Inline citation syntax is [source:ID].\nTASK: '
                                  + canonical(step["spec"]) + '\nDEPENDENCY OUTPUTS: ' + canonical(dependencies) + '\n' + context)
                    # Reserve every selected member before any transport starts.
                    reservation = await recovery.prepare(row, step, binding, prompt)
                    if reservation is None:
                        return
                    prepared.append((step, binding, prompt, reservation))

                async def run_step(work):
                    step, binding, prompt, reservation = work
                    attempt = reservation[0]
                    try:
                        await self.admission.revalidate(row)
                        recovered = await recovery.call(row, step, binding, prompt, prepared=reservation)
                        if recovered is None:
                            return "paused"
                        attempt, output, usage, episode, checks = recovered
                        checkpoint = await self.store.checkpoint_loop(row, step, attempt, output, usage)
                        if self.after_checkpoint:
                            await self.after_checkpoint(row, step["ordinal"])
                        return checkpoint
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _log_unexpected(row, step, attempt, exc)
                        exc._executor_logged = True
                        raise

                if not prepared:
                    continue
                tasks = [asyncio.create_task(run_step(work)) for work in prepared]
                try:
                    checkpoints = await asyncio.gather(*tasks)
                    if "completed" in checkpoints or "paused" in checkpoints:
                        return
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            try:
                await self.store.release(row)
            except Exception:
                pass
            raise
        except Exception as exc:
            if not getattr(exc, "_executor_logged", False):
                _log_unexpected(row, step, None, exc)
            try:
                settle = getattr(self.store, "settle_revocation", None)
                if settle and await settle(row):
                    return
                await self.store.stop(row, exc.code if isinstance(exc, ExecutionError) else type(exc).__name__, unknown=True)
            except Exception:
                log.warning("Loop %s awaits durable reconciliation", row["id"], exc_info=True)
        finally:
            required.reset(token)
