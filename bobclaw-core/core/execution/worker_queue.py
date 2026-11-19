"""One durable lane queue for synchronous and async worker client adapters."""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import logging
import threading
import uuid

from .job_store import JobStore, TERMINAL
from .jobs import new_execution_id
from core.worker.contract import WorkerUnavailable

log = logging.getLogger(__name__)


class WorkerQueue:
    def __init__(self, store, transport, *, poll=.1):
        self.store, self.transport, self.poll = store, transport, poll
        self.controller = uuid.uuid4().hex
        self.task = None
        self.active = None

    def start(self):
        self.task = asyncio.create_task(self.pump(), name='worker-lane-queue')

    async def close(self):
        if self.active:
            from .jobs import JobContext
            row = self.active
            await self.store.cancel(row['execution_id'], JobContext(row['owner'], row['principal'], row['caller_group'],
                                    row['boundary'], row['run_id'], row['run_generation']))
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def submit(self, context, request, *, execution_id=None, on_wait=None, reconcile=None):
        execution_id = execution_id or new_execution_id()
        try:
            await self.store.admit(context, execution_id, request)
            while True:
                row = await self.store.get(execution_id)
                if row.get('paused_unknown') and row['outcome'] == 'queued':
                    raise WorkerUnavailable('worker step paused by prior unknown outcome; execution_id=' + execution_id)
                if row['outcome'] in TERMINAL:
                    if row['outcome'] == 'unknown':
                        raise WorkerUnavailable('worker outcome unknown; affected step paused; execution_id=' + execution_id)
                    if row['outcome'] == 'timed_out' and reconcile is not None:
                        # Only a registered service adapter may supply this callable.
                        # It owns the workspace lock, current hash and effect checks.
                        try:
                            proof, current_digest = await asyncio.to_thread(reconcile, row)
                        except Exception as exc:
                            await self.store.block_timeout(context,execution_id,"job_reconciliation_failed:"+type(exc).__name__)
                            raise WorkerUnavailable("job continuation paused: adapter reconciliation failed") from exc
                        continued = await self.store.continue_timeout(context, execution_id, proof, current_workspace_digest=current_digest)
                        execution_id = continued['execution_id']
                        continue
                    if row['outcome'] == 'timed_out' and reconcile is None:
                        await self.store.block_timeout(context,execution_id,'job_reconciliation_required')
                    receipt = row['receipt'] or {}
                    if row['outcome'] == 'cancelled':
                        receipt = {**receipt, 'returncode': receipt.get('returncode', -9), 'killed': True, 'reason': 'cancelled'}
                    return {**receipt, 'outcome': row['outcome'], 'execution_id':execution_id,
                            'continuation_requires_reconciliation':row['outcome']=='timed_out',
                            'stdout': base64.b64decode(receipt.get('stdout_b64', '')),
                            'stderr': base64.b64decode(receipt.get('stderr_b64', ''))}
                if on_wait:
                    try:
                        await asyncio.to_thread(on_wait, await self.store.visible(execution_id))
                    except Exception:
                        log.warning('Worker wait observer failed', exc_info=True)
                await asyncio.sleep(self.poll)
        except asyncio.CancelledError:
            # Commit ownership-bound intent before the execution owner is closed.
            await asyncio.shield(self.store.cancel(execution_id, context))
            raise

    async def control(self, op, slot, execution_id=None):
        value = dict(version=2, op=op, lane_generation=slot['generation'], controller_session=slot['controller_session'])
        if execution_id:
            value['execution_id'] = execution_id
        return await asyncio.to_thread(self.transport.control, value)

    async def recover(self, c, slot):
        # 1 Fence submissions. 2 Inspect. 3 Cancel abandoned ownership.
        hello = await asyncio.to_thread(self.transport.hello)
        inspected = None
        if slot['active_execution']:
            inspected = await self.control('inspect', slot, slot['active_execution'])
        abandoned = hello.get('active_execution')
        if abandoned:
            await self.control('cancel', slot, abandoned)
        # 4 Prove idle, tied to the current controller and epoch.
        for _ in range(150):
            barrier = await self.control('idle_barrier', slot)
            if barrier.get('type') == 'idle_barrier':
                break
            await asyncio.sleep(self.poll)
        else:
            raise WorkerUnavailable('worker blocked_by_cleanup')
        await self.store.barrier(c, slot, barrier)
        slot['daemon_epoch'] = barrier['daemon_epoch']
        # 5 Reconcile receipts. 6 Settle once; never replay a dispatch intent.
        if slot['active_execution']:
            row = await self.store.get(slot['active_execution'])
            inspected = await self.control('inspect', slot, slot['active_execution'])
            receipt = inspected.get('receipt')
            if not receipt:
                receipt = {k: row[k] for k in ('execution_id', 'request_digest', 'lane_generation', 'controller_session')}
                receipt.update(type='result', outcome='unknown', cleanup='idle_proved',
                               daemon_epoch=barrier['daemon_epoch'], reason='terminal_history_missing')
            await self.store.enforce_active(c, row)
            await self.store.settle(c, row, receipt, slot, recovered=True)
            slot['active_execution'] = None
        return hello

    async def pump(self):
        while True:
            stop = threading.Event()
            try:
                async with self.store.pool.acquire() as c:
                    # Policy/grant cancellation must drain even a caller paused
                    # for timeout/unknown reconciliation. select settles these
                    # entries without authorizing or dispatching another child.
                    pending = await c.fetchval('''SELECT EXISTS(SELECT 1 FROM worker_queue q
                        JOIN worker_job_attempts a ON (a.lane,a.execution_id)=(q.lane,q.execution_id)
                        JOIN worker_callers g ON (g.lane,g.owner,g.principal,g.caller_group)=(a.lane,a.owner,a.principal,a.caller_group)
                        WHERE q.lane=$1 AND (a.cancel_requested OR
                        (NOT g.paused_unknown AND (g.paused_timeout IS NULL OR a.parent_execution_id=g.paused_timeout)))) OR
                        EXISTS(SELECT 1 FROM worker_lane_slots WHERE lane=$1 AND active_execution IS NOT NULL)''', self.store.lane)
                    if pending and await c.fetchval('SELECT pg_try_advisory_lock($1)', self.store.controller_lock):
                        try:
                            observed = await asyncio.to_thread(self.transport.hello)
                            slot = await self.store.takeover(c, self.controller, observed['lane_generation'] + 1)
                            hello = await self.recover(c, slot)
                            row = await self.store.select(c, slot, hello['profiles'], max_timeout=hello.get('max_timeout',900))
                            if row:
                                self.active = row
                                execution = asyncio.create_task(asyncio.to_thread(self.transport.submit, row['request'], stop))
                                try:
                                    while not execution.done():
                                        await asyncio.sleep(self.poll)
                                        if execution.done():
                                            break
                                        # Ping the lock connection. A DB outage closes the
                                        # owning socket; another controller must recover.
                                        await c.fetchval('SELECT 1')
                                        state = await self.store.get(row['execution_id'])
                                        if not await self.store.enforce_active(c, row):
                                            state['cancel_requested'] = True
                                        if state['cancel_requested']:
                                            await self.control('cancel', slot, row['execution_id'])
                                    receipt = await execution
                                    await self.store.settle(c, row, receipt, slot)
                                finally:
                                    stop.set()
                                    await asyncio.gather(execution, return_exceptions=True)
                                    self.active = None
                        finally:
                            await c.execute('SELECT pg_advisory_unlock($1)', self.store.controller_lock)
            except asyncio.CancelledError:
                stop.set()
                raise
            except Exception:
                stop.set()
                log.warning('Worker queue awaits recovery; no automatic resubmission', exc_info=True)
            await asyncio.sleep(self.poll)


class QueueBridge:
    """Dedicated DB/event-loop thread supports every existing sync call site.

    Async adapters wrap the same future; cancelling their await cancels the
    admission observer, which persists cancellation intent. The pump owns IO.
    """
    def __init__(self, cfg, dsn):
        self.cfg, self.dsn = cfg, dsn
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self.run, name='worker-queue-bridge', daemon=True)
        self.thread.start()

    def run(self):
        asyncio.run(self.main())

    async def main(self):
        import asyncpg
        from core.worker.client import Transport
        self.loop = asyncio.get_running_loop()
        self.shutdown_event = asyncio.Event()
        pool = None
        try:
            pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=6, timeout=5, command_timeout=10)
            from .api import lane_id
            from .job_reconciliation import verify_installed_workspace
            store = JobStore(pool, lane_id(), workspace_verifier=verify_installed_workspace)
            await store.initialize()
            self.queue = WorkerQueue(store, Transport(self.cfg))
            self.queue.start()
        except Exception as exc:
            self.ready.set_exception(WorkerUnavailable('worker queue storage unavailable: ' + type(exc).__name__))
            if pool:
                await pool.close()
            return
        self.ready.set_result(True)
        try:
            await self.shutdown_event.wait()
        finally:
            try:
                await self.queue.close()
            finally:
                await pool.close()

    def close(self):
        try:
            self.ready.result(timeout=15)
        except Exception:
            return
        self.loop.call_soon_threadsafe(self.shutdown_event.set)
        self.thread.join(timeout=15)

    def submit(self, context, request, *, on_wait=None, reconcile=None, execution_id=None):
        self.ready.result(timeout=15)
        async def observe():
            if not hasattr(self, 'observers'):
                self.observers = {}
            task = asyncio.current_task()
            if execution_id:
                self.observers[execution_id] = task
            try:
                return await self.queue.submit(context, request, execution_id=execution_id, on_wait=on_wait, reconcile=reconcile)
            finally:
                if execution_id:
                    self.observers.pop(execution_id, None)
        return asyncio.run_coroutine_threadsafe(observe(), self.loop)

    def cancel_and_wait(self, execution_id, context, observer):
        async def cleanup():
            # Cancellation completion is a receipt observation, not Future.cancel.
            async with asyncio.timeout(35):
                task = getattr(self, 'observers', {}).get(execution_id)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                while True:
                    async with self.queue.store.pool.acquire() as c:
                        ids = await c.fetch('SELECT execution_id FROM worker_job_attempts WHERE lane=$1 AND (execution_id=$2 OR logical_job_id=$2)', self.queue.store.lane,execution_id)
                    rows = []
                    for item in ids:
                        await self.queue.store.cancel(item['execution_id'], context)
                        rows.append(await self.queue.store.get(item['execution_id']))
                    if all(r['outcome'] in TERMINAL and r['cleanup'] == 'idle_proved' for r in rows):
                        return rows
                    await asyncio.sleep(self.queue.poll)
        return asyncio.run_coroutine_threadsafe(cleanup(), self.loop)
