"""Generate BoB's local secrets (BOBCLAW_SECRET / BOBCLAW_PASSWORD_HASH / TOTP_SECRET).

Writes cryptographically random values to `.secrets/bobclaw.env`, preserving any
existing real values (idempotent). Use `--force` to overwrite real values.

The admin password is stored as a **bcrypt hash only** (BOBCLAW_PASSWORD_HASH); the
plaintext is printed once at generation time and never written to disk.

Usage:
    python scripts/gen_secrets.py            # fill placeholders / empty keys only
    python scripts/gen_secrets.py --dry-run  # preview without writing
    python scripts/gen_secrets.py --force    # overwrite even non-placeholder values

Generated values:
  - BOBCLAW_SECRET       : 43 url-safe chars (256 bits) — JWT signing / scope-vouch key
  - BOBCLAW_PASSWORD_HASH : bcrypt hash of a random 24-char admin password (printed once)
  - TOTP_SECRET          : 32 base32 chars (160 bits) — TOTP seed
"""
from __future__ import annotations

import argparse
import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envfile import env_path, is_placeholder, load, update  # noqa: E402

# Token-style secrets stored verbatim. The admin PASSWORD is handled separately in
# main(): only its bcrypt hash (BOBCLAW_PASSWORD_HASH) is written to disk; the plaintext
# is printed once so it is never stored at rest.
GENERATORS = {
    "BOBCLAW_SECRET":   lambda: secrets.token_urlsafe(32),
    "TOTP_SECRET":      lambda: base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
}


def _bcrypt_hash(plain: str) -> str:
    try:
        import bcrypt
    except ImportError:
        print(
            "ERROR: bcrypt is not installed. Install the gateway deps first "
            "(uv pip install -r bobclaw-gateway/requirements.lock), then re-run.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _password_configured(env: dict) -> bool:
    """True if a real admin password (hash or legacy plaintext) is already set."""
    h = env.get("BOBCLAW_PASSWORD_HASH", "")
    p = env.get("BOBCLAW_PASSWORD", "")
    return (bool(h) and not is_placeholder(h)) or (bool(p) and not is_placeholder(p))


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

    # Admin password: generate a plaintext, store ONLY its bcrypt hash, print it once.
    new_password = None
    if _password_configured(env) and not args.force:
        skipped.append("BOBCLAW_PASSWORD_HASH")
    else:
        new_password = secrets.token_urlsafe(18)

    if args.dry_run:
        print(f"target: {env_path()}")
        for k in updates:
            masked = updates[k][:4] + "..." + updates[k][-4:]
            print(f"  would set {k}={masked}  ({len(updates[k])} chars)")
        if new_password:
            print("  would set BOBCLAW_PASSWORD_HASH=<bcrypt hash of a new random password>")
        for k in skipped:
            print(f"  would skip {k} (already set; pass --force to overwrite)")
        if not updates and not skipped and not new_password:
            print("  (nothing to do)")
        return 0

    if new_password:
        updates["BOBCLAW_PASSWORD_HASH"] = _bcrypt_hash(new_password)

    if updates:
        changes = update(updates)
        for c in changes:
            print(c)
    if skipped:
        print(f"skipped (already set): {', '.join(skipped)}  — use --force to overwrite")
    if not updates and not skipped:
        print("nothing to generate")

    if new_password:
        print("")
        print("  ================ ADMIN LOGIN (shown once) ================")
        print("   username: admin")
        print(f"   password: {new_password}")
        print("   Store this now — only its bcrypt hash is saved to disk.")
        print("  ==========================================================")
        # Machine-parseable line for install-bob.ps1 to capture (not for humans):
        print(f"BOBCLAW_LOGIN_PASSWORD={new_password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
