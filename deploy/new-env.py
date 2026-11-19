#!/usr/bin/env python3
"""Write a fresh private env file from the repository's example, exclusively."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

TEMPLATE = Path(__file__).resolve().parents[1] / ".secrets" / "bobclaw.env.example"


def new_env(out_file: Path) -> None:
    replacements = {
        "BOBCLAW_SECRET": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "TOTP_SECRET": base64.b32encode(secrets.token_bytes(20)).decode("ascii"),
    }
    seen = set()
    lines = []
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if match:
            key, value = match.groups()
            if key in replacements:
                value = replacements[key]
                seen.add(key)
            elif key == "POSTGRES_URL":
                # Compose and the application must use the same new password.
                url = urlsplit(value)
                user, separator, _ = url.netloc.rpartition("@")
                if not separator or ":" not in user:
                    raise ValueError("example POSTGRES_URL must include username and password")
                netloc = user.split(":", 1)[0] + ":" + replacements["POSTGRES_PASSWORD"] + "@" + url.netloc.rsplit("@", 1)[1]
                value = urlunsplit(url._replace(netloc=netloc))
            elif key.endswith(("_API_KEY", "_CLIENT_ID", "_CLIENT_SECRET", "_REFRESH_TOKEN")):
                value = ""
            line = key + "=" + value
        lines.append(line)
    if seen != replacements.keys():
        raise ValueError("example is missing required secret fields")
    # O_EXCL also refuses symlinks; permissions are private from creation time.
    fd = os.open(out_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_file", type=Path)
    args = parser.parse_args()
    try:
        new_env(args.out_file)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"new-env: {exc}\n")
    print(f"Created {args.out_file} (0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
