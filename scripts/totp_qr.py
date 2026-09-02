"""Show the admin 2FA enrollment QR for this install.

Renders the otpauth:// URI for the TOTP_SECRET already in `.secrets/bobclaw.env`
as a QR code — scan it with any authenticator app (Google Authenticator, Aegis,
1Password, ...). Does NOT create or rotate secrets (that is gen_secrets.py's job,
which shows this QR automatically when it generates a fresh TOTP_SECRET).

Usage:
    python scripts/totp_qr.py              # QR in the terminal + the URI
    python scripts/totp_qr.py --png PATH   # also write a PNG (keep it under .secrets/)

Requires `segno` (pure-Python, no dependencies):
    uv pip install --python .venv\\Scripts\\python.exe segno
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envfile import is_placeholder, load  # noqa: E402

ISSUER = "BoB"
ACCOUNT = "admin"


def otpauth_uri(secret: str, issuer: str = ISSUER, account: str = ACCOUNT) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"


def print_totp_qr(secret: str, png: str | None = None) -> int:
    """Print the enrollment QR (and optionally save a PNG). Returns exit code."""
    uri = otpauth_uri(secret)
    try:
        import segno
    except ImportError:
        print("  segno is not installed — cannot render the QR here.")
        print("    uv pip install --python .venv\\Scripts\\python.exe segno")
        print("  Enroll manually instead (authenticator 'enter a setup key', or paste the URI):")
        print(f"    {uri}")
        return 1

    qr = segno.make(uri, error="m")
    if png:
        qr.save(png, scale=8, border=2)
        print(f"  PNG written: {png}  (contains the TOTP secret — keep it private)")
    print("")
    print(f"  Scan with your authenticator app (issuer: {ISSUER}, account: {ACCOUNT}):")
    print("")
    # The Windows console default (cp1252) cannot encode the QR's block characters.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        try:
            qr.terminal(compact=True, border=2)
        except TypeError:  # older segno without compact=
            qr.terminal(border=2)
    except UnicodeEncodeError:
        print("  (terminal cannot render the QR — use the PNG or the URI below)")
    print(f"  URI: {uri}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--png", help="Also write the QR as a PNG to this path")
    args = ap.parse_args()

    env = load()
    secret = env.get("TOTP_SECRET", "")
    if not secret or is_placeholder(secret):
        print(
            "No real TOTP_SECRET in .secrets/bobclaw.env — run scripts/gen_secrets.py first.",
            file=sys.stderr,
        )
        return 2
    return print_totp_qr(secret, png=args.png)


if __name__ == "__main__":
    raise SystemExit(main())
