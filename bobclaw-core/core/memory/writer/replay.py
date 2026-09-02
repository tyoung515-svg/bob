"""Checkpointed historical replay for the W3 T0 writer."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from core.memory.writer.tasks import ProjectVerbatimTask


@dataclass
class ReplayStats:
    visited: int = 0
    projected: int = 0
    chunks: int = 0
    already_completed: int = 0
    duplicates: int = 0
    disabled: int = 0


async def replay_t0(event_log, task: ProjectVerbatimTask, *, limit: int | None = None) -> ReplayStats:
    """Resume after the last durable checkpoint and stop before busy work."""
    await task.ledger.initialize()
    checkpoint = await task.ledger.get_checkpoint(task.task_version, task.prompt_version)
    since = checkpoint.last_event_id if checkpoint else None
    processed_count = checkpoint.processed_count if checkpoint else 0
    stats = ReplayStats()

    async for event in event_log.replay(since_event_id=since):
        if limit is not None and stats.visited >= limit:
            break
        result = await task.project(event)
        if result.status == "busy":
            raise RuntimeError(
                f"event {event.event_id} is being projected by another writer; checkpoint not advanced"
            )
        stats.visited += 1
        if result.status == "completed":
            stats.projected += 1
            stats.chunks += result.chunk_count
        elif result.status == "duplicate":
            stats.duplicates += 1
        elif result.status == "already_completed":
            stats.already_completed += 1
        else:
            stats.disabled += 1
        processed_count += 1
        await task.ledger.advance_checkpoint(
            task_version=task.task_version,
            prompt_version=task.prompt_version,
            last_event_id=event.event_id,
            processed_count=processed_count,
        )
    return stats


async def _run_from_bootstrap(limit: int | None) -> dict:
    from core.config import config
    from core.memory.bootstrap import MemoryBootstrapConfig, bootstrap_memory

    memory = bootstrap_memory(MemoryBootstrapConfig.from_env(config))
    if memory.writer is None:
        raise RuntimeError(
            "W3 writer is disabled; set MEMORY_WRITER_ENABLED=true and arm MEMORY_WRITE_FENCE_ENABLED"
        )
    stats = await replay_t0(memory.event_log, memory.writer, limit=limit)
    return {
        **asdict(stats),
        "ledger_rows": await memory.completion_ledger.count_completed(
            task_version=memory.writer.task_version,
            prompt_version=memory.writer.prompt_version,
        ),
        "ledger_chunks": await memory.completion_ledger.completed_chunk_count(
            task_version=memory.writer.task_version,
            prompt_version=memory.writer.prompt_version,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay L0 events through the W3 T0 writer")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run_from_bootstrap(args.limit)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
