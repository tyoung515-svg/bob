"""Worker for the P2 two-Windows-principal write-fence evidence script."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from core.ledger.federation import FederationRegistry
from core.memory.write_fence import WriteFence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-dir", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--role", choices=("holder", "once"), required=True)
    parser.add_argument("--release-file")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    os.environ["BOBCLAW_WRITE_FENCE_LOCK_DIR"] = args.lock_dir
    registry = FederationRegistry(Path(args.registry)).load()
    fence = WriteFence(
        registry,
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    status = {
        "degraded": fence.degraded,
        "reason": fence.degraded_reason,
        "resource": fence.resource_identity,
        "status": "degraded" if fence.degraded else "acquired",
    }
    Path(args.status_file).write_text(json.dumps(status), encoding="utf-8")
    print(json.dumps(status), flush=True)

    if args.role != "holder" or fence.degraded:
        return 0
    if not args.release_file:
        raise SystemExit("holder requires --release-file")

    deadline = time.monotonic() + args.timeout
    while not Path(args.release_file).exists():
        if time.monotonic() >= deadline:
            raise SystemExit("holder timed out waiting for release")
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
