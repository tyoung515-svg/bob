"""Generate BoBClaw's local secrets (BOBCLAW_SECRET / BOBCLAW_PASSWORD / TOTP_SECRET).

Writes cryptographically random values to `.secrets/bobclaw.env`, preserving any
existing real values (idempotent). Use `--force` to overwrite real values.

Usage:
    python scripts/gen_secrets.py            # fill placeholders / empty keys only
    python scripts/gen_secrets.py --dry-run  # preview without writing
    python scripts/gen_secrets.py --force    # overwrite even non-placeholder values

Generated values:
  - BOBCLAW_SECRET  : 43 url-safe chars (256 bits) — JWT signing fallback
  - BOBCLAW_PASSWORD: 32 url-safe chars (192 bits) — admin password
  - TOTP_SECRET     : 32 base32 chars  (160 bits) — TOTP seed
"""
from __future__ import annotations

import argparse
import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envfile import env_path, is_placeholder, load, update  # noqa: E402

GENERATORS = {
    "BOBCLAW_SECRET":   lambda: secrets.token_urlsafe(32),
    "BOBCLAW_PASSWORD": lambda: secrets.token_urlsafe(24),
    "TOTP_SECRET":      lambda: base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true",
                    help="Overwrite even non-placeholder values")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change; do not write")
    args = ap.parse_args()

    env = load()
    updates: dict[str, str] = {}
    skipped: list[str] = []

    for key, gen in GENERATORS.items():
        existing = env.get(key, "")
        if existing and not is_placeholder(existing) and not args.force:
            skipped.append(key)
            continue
        updates[key] = gen()

    if args.dry_run:
        print(f"target: {env_path()}")
        for k in updates:
            masked = updates[k][:4] + "..." + updates[k][-4:]
            print(f"  would set {k}={masked}  ({len(updates[k])} chars)")
        for k in skipped:
            print(f"  would skip {k} (already set; pass --force to overwrite)")
        if not updates and not skipped:
            print("  (nothing to do)")
        return 0

    if updates:
        changes = update(updates)
        for c in changes:
            print(c)
    if skipped:
        print(f"skipped (already set): {', '.join(skipped)}  — use --force to overwrite")
    if not updates and not skipped:
        print("nothing to generate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
