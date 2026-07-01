"""BoBClaw Core — MCP stdio entrypoint (Neck Beard MODE).

Publishes Bob as an MCP server over stdio so an external agent can drive it
headlessly. Mirrors ``start.py``'s run shape.

Run (from bobclaw-core/, stack up, with a scoped agent token):

    BOBCLAW_AGENT_TOKEN=<jwt> BOBCLAW_GATEWAY=http://127.0.0.1:7826 \
        python mcp_serve.py

Register in an MCP client (e.g. Claude Code) as a stdio server pointing at this
file with BOBCLAW_AGENT_TOKEN in its env. Logs go to STDERR (stdout is the MCP
protocol channel and must not be polluted).
"""
from __future__ import annotations

import logging
import sys

from core.mcp.server import build_server, load_config


def main() -> None:
    # STDERR only — stdout carries the MCP JSON-RPC frames.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()
    logging.getLogger("bobclaw.mcp").info(
        "BoBClaw MCP server (stdio) -> gateway=%s faces=%s council=%s",
        cfg.gateway,
        cfg.faces or "(unrestricted)",
        cfg.council_allowed,
    )
    build_server(cfg).run(transport="stdio")


if __name__ == "__main__":
    main()
