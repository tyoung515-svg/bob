"""``python -m bobclaw_tui`` — launch the flight cockpit.

Login: password + TOTP from the gateway's .secrets/bobclaw.env via POST /auth/login
(cached in .secrets/tui-token.json, refreshed via /auth/refresh on expiry). --token is
an explicit override for tests/manual use. Gateway host:port via --gateway or
$BOBCLAW_GATEWAY.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from bobclaw_tui.app import DEFAULT_GATEWAY, run
from bobclaw_tui.auth import get_access_token


def main() -> int:
    p = argparse.ArgumentParser(description="BoBClaw flight cockpit (TUI)")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY, help="host:port of the gateway")
    p.add_argument("--token", default=None, help="JWT override; else login via /auth/login")
    p.add_argument("--flight", default=None, help="filter the monitor to one flight_id")
    args = p.parse_args()
    token = args.token or asyncio.run(get_access_token(args.gateway))
    run(args.gateway, token, args.flight)
    return 0


if __name__ == "__main__":
    sys.exit(main())
