"""Chunk-audit reduce — the 1:N manager/auditor tier (JOAT centerpiece demo).

After a wide fan-out, partition the worker results into fixed-size chunks and run
ONE auditor per chunk (1:N, default 1:10) — deliberately distinct from the
per-worker (1:1) critic node. The auditors run concurrently; their verdicts plus
the raw results feed the apex synthesizer.

The audit callable is INJECTED, so this primitive is backend-agnostic and fully
unit-testable without network: the demo passes a real GLM-5.2 call; tests pass a
stub. One auditor failing is captured as an error verdict, never sinking the
other chunks' audits.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

# audit_fn(chunk_index, chunk_results) -> verdict dict
AuditFn = Callable[[int, list], Awaitable[dict]]


def partition(results: list, chunk_size: int = 10) -> list[list]:
    """Split *results* into consecutive chunks of at most *chunk_size*.

    100 results, size 10 → 10 chunks of 10. A non-divisible tail forms a smaller
    final chunk (never dropped). ``chunk_size`` must be >= 1.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    return [results[i : i + chunk_size] for i in range(0, len(results), chunk_size)]


async def chunk_audit_reduce(
    worker_results: list,
    audit_fn: AuditFn,
    *,
    chunk_size: int = 10,
) -> list[dict]:
    """Partition *worker_results* into chunks and audit each chunk concurrently.

    Returns one verdict per chunk, ORDERED by chunk index (``gather`` preserves
    input order regardless of completion order). Each verdict is wrapped with its
    ``chunk_index`` and the ``reviewed`` count so the synth + telemetry can prove
    the 1:N ratio and that every worker result was covered exactly once.

    An ``audit_fn`` that raises is captured as an error verdict (``verdict: None``)
    rather than sinking the whole reduce — one bad auditor must not lose the rest.
    """
    chunks = partition(worker_results, chunk_size)

    async def _run(idx: int, chunk: list) -> dict:
        try:
            verdict = await audit_fn(idx, chunk)
        except Exception as exc:  # noqa: BLE001 - isolate a single auditor failure
            return {"chunk_index": idx, "reviewed": len(chunk), "verdict": None, "error": str(exc)}
        return {"chunk_index": idx, "reviewed": len(chunk), "verdict": verdict}

    return list(await asyncio.gather(*(_run(i, c) for i, c in enumerate(chunks))))
