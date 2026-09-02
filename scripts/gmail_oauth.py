"""Run the Gmail OAuth2 installed-app flow and write the refresh token to .env.

Prerequisite: a `credentials.json` downloaded from Google Cloud Console
(APIs & Services → Credentials → OAuth 2.0 Client IDs → Desktop app).
Default location: `.secrets/credentials.json` (BoBClaw root). Override with `--credentials`.

Required Python package (not in requirements.txt — install ad-hoc):
    pip install google-auth-oauthlib

Usage:
    python scripts/gmail_oauth.py
    python scripts/gmail_oauth.py --credentials path/to/credentials.json
    python scripts/gmail_oauth.py --token-out token.json   # also write Google-style token file
    python scripts/gmail_oauth.py --scopes readonly,send   # short-name comma list

The script writes GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, and GMAIL_REFRESH_TOKEN
back to .secrets/bobclaw.env. Existing values are overwritten — re-running
authorizes a fresh refresh token.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envfile import env_path, repo_root, update  # noqa: E402

# Short names → full Gmail OAuth scope URLs.
# Default covers the common LLM-agent uses: read, send, modify (e.g., labels).
SCOPE_ALIASES = {
    "readonly": "https://www.googleapis.com/auth/gmail.readonly",
    "send":     "https://www.googleapis.com/auth/gmail.send",
    "modify":   "https://www.googleapis.com/auth/gmail.modify",
    "compose":  "https://www.googleapis.com/auth/gmail.compose",
    "labels":   "https://www.googleapis.com/auth/gmail.labels",
    "metadata": "https://www.googleapis.com/auth/gmail.metadata",
    "full":     "https://mail.google.com/",
}
DEFAULT_SCOPES = "readonly,send,modify"


def resolve_scopes(spec: str) -> list[str]:
    out: list[str] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("http"):
            out.append(tok)
        elif tok in SCOPE_ALIASES:
            out.append(SCOPE_ALIASES[tok])
        else:
            raise SystemExit(
                f"unknown scope alias: {tok!r}\n"
                f"available: {', '.join(SCOPE_ALIASES)}\n"
                f"or pass a full https://www.googleapis.com/auth/... URL"
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--credentials", default=str(repo_root() / ".secrets" / "credentials.json"),
                    help="Path to OAuth client credentials JSON (default: .secrets/credentials.json)")
    ap.add_argument("--scopes", default=DEFAULT_SCOPES,
                    help=f"Comma-separated scope aliases or URLs (default: {DEFAULT_SCOPES})")
    ap.add_argument("--token-out", default=None,
                    help="Optional: also write a Google-style token.json to this path")
    ap.add_argument("--port", type=int, default=0,
                    help="Local server port for the OAuth callback (0 = pick free)")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        print(
            "ERROR: google-auth-oauthlib is not installed.\n"
            "Install it with:  pip install google-auth-oauthlib\n"
            "(it is intentionally not in requirements.txt — only needed for setup)",
            file=sys.stderr,
        )
        return 2

    cred_path = Path(args.credentials)
    if not cred_path.exists():
        print(
            f"ERROR: credentials file not found: {cred_path}\n"
            "Download from Google Cloud Console → APIs & Services → Credentials\n"
            "→ your OAuth 2.0 Client ID → DOWNLOAD JSON.",
            file=sys.stderr,
        )
        return 2

    scopes = resolve_scopes(args.scopes)
    print(f"requesting scopes: {scopes}")

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes)
    creds = flow.run_local_server(port=args.port)

    if not creds.refresh_token:
        print(
            "ERROR: OAuth flow completed but no refresh_token was returned.\n"
            "Possible cause: this account previously authorized this client.\n"
            "Fix: revoke at https://myaccount.google.com/permissions and retry.",
            file=sys.stderr,
        )
        return 1

    raw = json.loads(cred_path.read_text(encoding="utf-8"))
    installed = raw.get("installed") or raw.get("web") or {}
    client_id = installed.get("client_id", "")
    client_secret = installed.get("client_secret", "")

    updates = {
        "GMAIL_CLIENT_ID": client_id,
        "GMAIL_CLIENT_SECRET": client_secret,
        "GMAIL_REFRESH_TOKEN": creds.refresh_token,
    }
    changes = update(updates)
    print(f"wrote to {env_path()}:")
    for c in changes:
        print(f"  {c}")

    if args.token_out:
        Path(args.token_out).write_text(creds.to_json(), encoding="utf-8")
        print(f"also wrote Google-style token file: {args.token_out}")

    print("\nDone. The refresh token does not expire unless revoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
