"""
BoBClaw Core — Council protocol loader/parser.

Reads the COUNCIL-OS-v1.0 markdown protocol document and parses it into a
structured dict the engine can splice into voice prompts. Pure stdlib (``re``,
``pathlib``) — no Bob coupling, no third-party deps.

Exposed API:
- ``load_protocols(path) -> dict`` — read + parse a protocol file (or fall back
  to the built-in summary when the file is missing).
- ``DEFAULT_PROTOCOL_PATH`` — the in-tree COUNCIL-OS-v1.0.md, resolved relative
  to this module.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# In-tree protocol doc, resolved relative to this module (NOT a hardcoded
# Desktop/ForestOS path). P1b may override via the engine's protocol_path arg.
DEFAULT_PROTOCOL_PATH: Path = Path(__file__).resolve().parent / "protocols" / "COUNCIL-OS-v1.0.md"

# Matches a tagged protocol heading, e.g.  ### `[PROT-01]` Delta-Only Messaging
_PROTOCOL_HEADING_RE = re.compile(r"###\s+`(\[[\w-]+\])`\s+(.+?)$", re.MULTILINE)

_PROTOCOLS_SUMMARY_TEMPLATE = """\
Active protocols from COUNCIL-OS v1.0:
[PROT-01] Delta-Only Messaging — state only new content, no restating
[PROT-02] Direct Citation — quote specific text when challenging a prior voice
[PROT-03] Falsifiable Prompts — state load-bearing assumptions, not confidence levels
[ROLE-01] Designated Synthesizer — final voice: resolve IDs, produce HANDOFF block
[ROLE-02] Assumption Stress-Testing — hunt structural weaknesses in prior claims
[P-01] Full Chain Delivery — human passes complete history to every voice
"""

_HANDOFF_TEMPLATE = """\
### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** (List Idea IDs closed this round)
- **[ACTIVE DEBATE]:** (List Idea IDs currently being stress-tested)
- **[BLOCKED]:** (What we need from human to proceed)
- **[CORRECTION]:** (Any hallucination or error flags — omit section if none)
- **[NEXT TASK]:** (@NextVoice or @Human: specific directive)
"""


def parse_protocol_summary(raw: str) -> str:
    """Extract a condensed protocol summary: ID + one-line title per entry.

    Falls back to the built-in summary template when no tagged headings parse.
    """
    lines = [f"{m.group(1)} {m.group(2).strip()}" for m in _PROTOCOL_HEADING_RE.finditer(raw)]
    return "\n".join(lines) if lines else _PROTOCOLS_SUMMARY_TEMPLATE


def parse_protocol_ids(raw: str) -> dict[str, str]:
    """Return ``{id: title}`` for every tagged protocol in the document."""
    return {m.group(1): m.group(2).strip() for m in _PROTOCOL_HEADING_RE.finditer(raw)}


def load_protocols(path: str | Path | None = None) -> dict:
    """Load a COUNCIL-OS protocol doc and parse it into a structured dict.

    Args:
        path: Path to the protocol markdown. Defaults to the in-tree
            ``DEFAULT_PROTOCOL_PATH`` when ``None`` or empty.

    Returns:
        ``{"full_text", "summary", "handoff_template", "ids"}``. When the file
        is missing, ``full_text`` is empty and ``summary`` is the built-in
        template (fail-soft — never raises on a missing file).
    """
    resolved = Path(path) if path else DEFAULT_PROTOCOL_PATH
    if not resolved.exists():
        logger.warning("Protocol file not found at %r — using built-in summary", str(resolved))
        return {
            "full_text": "",
            "summary": _PROTOCOLS_SUMMARY_TEMPLATE,
            "handoff_template": _HANDOFF_TEMPLATE,
            "ids": {},
        }

    raw = resolved.read_text(encoding="utf-8", errors="ignore")
    return {
        "full_text": raw,
        "summary": parse_protocol_summary(raw),
        "handoff_template": _HANDOFF_TEMPLATE,
        "ids": parse_protocol_ids(raw),
    }
