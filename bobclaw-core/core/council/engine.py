"""
BoBClaw Core — Council Chain Delivery Engine.

Implements [P-01] Full Chain Delivery from COUNCIL-OS-v1.0.

Chain: Claude (voice 1) -> Gemini (voice 2) -> Local synthesis (voice 3).
If no local model: Claude synthesizes with neutrality instruction.

This is the self-contained, backend-injected core (CoCouncil P1a). It has NO
Bob coupling — no imports of core.graph / core.nodes / core.backends /
core.config. Backends are injected as async callables (the ``BackendFn`` seam);
P1b adapts Bob's real backends to that signature. Cost metering is a pluggable
hook (``cost_fn``) rather than a hardcoded price map; P1b injects Bob's
``core/backends/_cost.py``. Pure Python stdlib only.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from core.council.protocol import (
    DEFAULT_PROTOCOL_PATH,
    _HANDOFF_TEMPLATE,
    _PROTOCOLS_SUMMARY_TEMPLATE,
    load_protocols as _load_protocols,
)

logger = logging.getLogger(__name__)

# async (system: str, user_message: str) -> str  — the injection seam (P1b wires
# Bob's real backends to this signature; the engine never imports them).
BackendFn = Callable[[str, str], Awaitable[str]]

# Pluggable cost hook: (model/backend name, token count) -> usd.  Defaults to
# None (=> cost 0.0 / metering skipped).  P1b injects Bob's _cost metering.
CostFn = Callable[[str, int], float]

# Default log dir — relative + in-tree (NO hardcoded Desktop/ForestOS path).
# P1b sets the real path via the constructor.
DEFAULT_LOG_DIR: Path = Path("data/council-logs")

_COUNCIL_SYSTEM_BASE = (
    "You are participating in a ForestOS council session governed by COUNCIL-OS v1.0. "
    "Follow all ratified protocols strictly. "
    "Do not summarize or restate prior content — delta-only per [PROT-01]."
)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CouncilHandoff:
    """[PROT-06] COUNCIL HANDOFF block parsed from synthesis output."""
    resolved: list[str]        # Idea IDs closed this round
    active_debate: list[str]   # Idea IDs under stress-test
    blocked: list[str]         # What we need from human
    corrections: list[str]     # Hallucination flags
    next_task: str             # Next directive


@dataclass
class CouncilVoice:
    model: str
    role: str           # "claude", "gemini", "synthesizer"
    response: str
    tokens_used: int
    latency_ms: int


@dataclass
class CouncilSession:
    session_id: str
    topic: str
    voices: list[CouncilVoice]
    synthesis: str
    handoff: CouncilHandoff
    protocols_applied: list[str]
    timestamp: str
    total_tokens: int
    total_cost_estimate: float


# ── Engine ────────────────────────────────────────────────────────────────────

class CouncilEngine:
    """
    Orchestrates a three-voice council session per [P-01] Full Chain Delivery.

    Backends are injected for testability — pass mock callables in tests.
    Each backend has the signature: async (system: str, message: str) -> str

    Cost metering is an optional injected hook (``cost_fn``): given a model
    name and a token count it returns a USD estimate. When ``cost_fn`` is None
    (the default), per-session cost is 0.0 (metering skipped).
    """

    def __init__(
        self,
        claude_backend: BackendFn,
        gemini_backend: BackendFn,
        local_backend: Optional[BackendFn] = None,
        log_dir: Union[str, Path] = DEFAULT_LOG_DIR,
        protocol_path: Union[str, Path] = "",
        cost_fn: Optional[CostFn] = None,
    ):
        self.claude = claude_backend
        self.gemini = gemini_backend
        self.local = local_backend
        self._log_dir = log_dir
        self._protocol_path = protocol_path
        self._cost_fn = cost_fn
        self._session_counter = 0
        self._protocols: Optional[dict] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def load_protocols(self, protocol_path: Union[str, Path] = "") -> dict:
        """Load COUNCIL-OS-v1.0.md and parse into a structured protocol dict.

        An explicit ``protocol_path`` arg wins over the constructor default,
        which in turn falls back to the in-tree ``DEFAULT_PROTOCOL_PATH``.
        Caches the result after the first call (returns the same object), so
        a later file deletion does not change what's returned.
        """
        if self._protocols is not None:
            return self._protocols

        path = protocol_path or self._protocol_path or DEFAULT_PROTOCOL_PATH
        self._protocols = _load_protocols(path)
        return self._protocols

    async def run_session(self, topic: str, context: str = "") -> CouncilSession:
        """
        Full [P-01] chain delivery.

        Step 1 — Claude Voice:
          Prompt includes council protocols + topic + any prior context.
          Claude provides initial analysis per [PROT-01/02/03].

        Step 2 — Gemini Voice:
          Receives Claude's response + original topic.
          Gemini stress-tests Claude's assumptions per [ROLE-02] and [PROT-02].

        Step 3 — Synthesis:
          Local model synthesizes both voices per [ROLE-01].
          Falls back to Claude when no local model is available.
          Produces the COUNCIL HANDOFF block.
        """
        protocols = self.load_protocols()
        session_id = self._generate_session_id()
        timestamp = datetime.now(timezone.utc).isoformat()
        voices: list[CouncilVoice] = []

        # ── Step 1: Claude voice ──────────────────────────────────────────────
        claude_msg = self._format_claude_prompt(topic, protocols, context)
        t0 = time.monotonic()
        claude_resp = await self.claude(_COUNCIL_SYSTEM_BASE, claude_msg)
        claude_latency = int((time.monotonic() - t0) * 1000)
        claude_tokens = len(claude_resp) // 4 + len(claude_msg) // 4
        voices.append(CouncilVoice(
            model="claude-opus-4-6",
            role="claude",
            response=claude_resp,
            tokens_used=claude_tokens,
            latency_ms=claude_latency,
        ))
        logger.info("Council session %s — Claude voice complete (%d tokens)", session_id, claude_tokens)

        # ── Step 2: Gemini voice ──────────────────────────────────────────────
        gemini_msg = self._format_gemini_prompt(topic, claude_resp, protocols)
        t0 = time.monotonic()
        gemini_resp = await self.gemini(_COUNCIL_SYSTEM_BASE, gemini_msg)
        gemini_latency = int((time.monotonic() - t0) * 1000)
        gemini_tokens = len(gemini_resp) // 4 + len(gemini_msg) // 4
        voices.append(CouncilVoice(
            model="gemini-2.0-flash",
            role="gemini",
            response=gemini_resp,
            tokens_used=gemini_tokens,
            latency_ms=gemini_latency,
        ))
        logger.info("Council session %s — Gemini voice complete (%d tokens)", session_id, gemini_tokens)

        # ── Step 3: Synthesis ─────────────────────────────────────────────────
        synth_msg = self._format_synthesis_prompt(topic, claude_resp, gemini_resp, protocols)
        synth_backend = self.local if self.local is not None else self.claude
        synth_model = "local" if self.local is not None else "claude-opus-4-6"
        t0 = time.monotonic()
        synthesis = await synth_backend(_COUNCIL_SYSTEM_BASE, synth_msg)
        synth_latency = int((time.monotonic() - t0) * 1000)
        synth_tokens = len(synthesis) // 4 + len(synth_msg) // 4
        voices.append(CouncilVoice(
            model=synth_model,
            role="synthesizer",
            response=synthesis,
            tokens_used=synth_tokens,
            latency_ms=synth_latency,
        ))
        logger.info("Council session %s — Synthesis complete (%d tokens, model=%s)",
                    session_id, synth_tokens, synth_model)

        # ── Finalize ──────────────────────────────────────────────────────────
        handoff = self._extract_handoff(synthesis)
        total_tokens = sum(v.tokens_used for v in voices)
        total_cost = self._estimate_cost(voices)

        return CouncilSession(
            session_id=session_id,
            topic=topic,
            voices=voices,
            synthesis=synthesis,
            handoff=handoff,
            protocols_applied=["COUNCIL-OS-v1.0", "PROT-01", "PROT-02", "PROT-03",
                                "ROLE-01", "ROLE-02", "P-01"],
            timestamp=timestamp,
            total_tokens=total_tokens,
            total_cost_estimate=round(total_cost, 6),
        )

    def save_session_log(self, session: CouncilSession, log_dir: Union[str, Path, None] = None) -> str:
        """Write markdown session log. Returns the file path written.

        ``log_dir`` defaults to the engine's configured log dir. The directory
        is created on write if missing.
        """
        target = Path(log_dir) if log_dir is not None else Path(self._log_dir or DEFAULT_LOG_DIR)
        target.mkdir(parents=True, exist_ok=True)
        log_path = str(target / f"{session.session_id}.md")

        h = session.handoff
        resolved_str = ", ".join(h.resolved) if h.resolved else "None"
        debate_str = ", ".join(h.active_debate) if h.active_debate else "None"
        blocked_str = ", ".join(h.blocked) if h.blocked else "None"
        corrections_str = ", ".join(h.corrections) if h.corrections else "None"

        claude_voice = next((v for v in session.voices if v.role == "claude"), None)
        gemini_voice = next((v for v in session.voices if v.role == "gemini"), None)
        synth_voice = next((v for v in session.voices if v.role == "synthesizer"), None)

        content = f"""\
# Council Session {session.session_id}
**Topic:** {session.topic}
**Date:** {session.timestamp}
**Protocols:** {", ".join(session.protocols_applied)}

## Claude Voice
{claude_voice.response if claude_voice else "(no response)"}

## Gemini Voice
{gemini_voice.response if gemini_voice else "(no response)"}

## Synthesis
{synth_voice.response if synth_voice else "(no response)"}

## Council Handoff
- [RESOLVED]: {resolved_str}
- [ACTIVE DEBATE]: {debate_str}
- [BLOCKED]: {blocked_str}
- [CORRECTION]: {corrections_str}
- [NEXT TASK]: {h.next_task or "None"}

## Metrics
Total tokens: {session.total_tokens}
Estimated cost: ${session.total_cost_estimate:.6f}
"""
        Path(log_path).write_text(content, encoding="utf-8")
        logger.info("Council session log written: %s", log_path)
        return log_path

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _estimate_cost(self, voices: list[CouncilVoice]) -> float:
        """Sum per-voice cost via the injected ``cost_fn`` hook.

        Returns 0.0 when no ``cost_fn`` is configured (metering skipped) or
        when the hook raises for a given voice (fail-soft — one bad estimate
        must not abort a completed session).
        """
        if self._cost_fn is None:
            return 0.0
        total = 0.0
        for v in voices:
            try:
                total += float(self._cost_fn(v.model, v.tokens_used))
            except Exception:  # noqa: BLE001 — cost metering is best-effort
                logger.warning("cost_fn failed for model=%s; counting 0.0", v.model)
        return total

    def _format_claude_prompt(self, topic: str, protocols: dict, context: str) -> str:
        """Build Claude voice prompt with protocol instructions."""
        parts = [
            f"PROTOCOLS IN EFFECT:\n{protocols.get('summary', _PROTOCOLS_SUMMARY_TEMPLATE)}",
            f"\nTOPIC: {topic}",
        ]
        if context:
            parts.append(f"\nPRIOR COUNCIL CONTEXT:\n{context}")
        parts.append(
            "\nProvide your initial analysis as the first council voice. "
            "Follow [PROT-01] (new content only), [PROT-02] (cite when challenging), "
            "[PROT-03] (state load-bearing assumptions, not confidence levels).\n"
            f"End your response with:\n{_HANDOFF_TEMPLATE}"
        )
        return "\n".join(parts)

    def _format_gemini_prompt(self, topic: str, claude_response: str, protocols: dict) -> str:
        """Build Gemini voice prompt with Claude's output for stress-testing."""
        return (
            f"PROTOCOLS IN EFFECT:\n{protocols.get('summary', _PROTOCOLS_SUMMARY_TEMPLATE)}\n\n"
            f"TOPIC: {topic}\n\n"
            f"CLAUDE'S ANALYSIS:\n{claude_response}\n\n"
            "Per [ROLE-02], stress-test Claude's assumptions. Quote specific claims per [PROT-02]. "
            "State your own load-bearing assumptions per [PROT-03]. "
            "Do NOT restate what Claude said — delta-only per [PROT-01].\n"
            f"End your response with:\n{_HANDOFF_TEMPLATE}"
        )

    def _format_synthesis_prompt(
        self, topic: str, claude_voice: str, gemini_voice: str, protocols: dict
    ) -> str:
        """Build synthesis prompt per [ROLE-01] Designated Synthesizer."""
        return (
            f"PROTOCOLS IN EFFECT:\n{protocols.get('summary', _PROTOCOLS_SUMMARY_TEMPLATE)}\n\n"
            f"TOPIC: {topic}\n\n"
            f"CLAUDE'S ANALYSIS:\n{claude_voice}\n\n"
            f"GEMINI'S ANALYSIS:\n{gemini_voice}\n\n"
            "Per [ROLE-01] Designated Synthesizer: introduce NO new ideas. "
            "Resolve open Idea IDs — declare what was closed, what remains active. "
            "Prune dead threads. Maintain strict neutrality between voices.\n\n"
            "Your response MUST end with this exact block:\n"
            f"{_HANDOFF_TEMPLATE}"
        )

    def _extract_handoff(self, synthesis: str) -> CouncilHandoff:
        """Parse COUNCIL HANDOFF block from synthesis output.
        Returns empty CouncilHandoff gracefully on malformed output."""
        block_match = re.search(
            r"###\s+📋\s+COUNCIL HANDOFF\s*\n(.*?)(?=\n###|\Z)",
            synthesis,
            re.DOTALL | re.IGNORECASE,
        )
        if not block_match:
            logger.warning("No COUNCIL HANDOFF block found in synthesis output")
            return CouncilHandoff(
                resolved=[], active_debate=[], blocked=[], corrections=[], next_task=""
            )

        block = block_match.group(1)

        def _field(label: str) -> str:
            m = re.search(
                rf"\*\*\[{re.escape(label)}\]:\*\*\s*(.*?)(?=\n-\s+\*\*|\Z)",
                block, re.DOTALL,
            )
            return m.group(1).strip() if m else ""

        def _to_list(text: str) -> list[str]:
            if not text or text.lower().strip() in ("none", "n/a", "-", ""):
                return []
            items = re.split(r"[,\n]+", text)
            return [
                item.strip()
                for item in items
                if item.strip() and item.strip().lower() not in ("none", "n/a", "-")
            ]

        return CouncilHandoff(
            resolved=_to_list(_field("RESOLVED")),
            active_debate=_to_list(_field("ACTIVE DEBATE")),
            blocked=_to_list(_field("BLOCKED")),
            corrections=_to_list(_field("CORRECTION")),
            next_task=_field("NEXT TASK"),
        )

    def _generate_session_id(self) -> str:
        """``SESSION-{NNN}``, incrementing from the last logged session.

        Multi-process-safe: BoBClaw core runs multi-process (CLAUDE.md Topology),
        so two concurrent council turns could otherwise glob the same max and
        collide on one ``SESSION-NNN`` file (last-write-wins overwrite, one log
        lost). We RESERVE the id by atomically creating an empty placeholder with
        ``O_EXCL`` (``touch(exist_ok=False)``) and retrying the next number on
        collision; ``save_session_log`` later overwrites its own placeholder. The
        ``SESSION-NNN`` format is unchanged.
        """
        last_num = 0
        log_dir = Path(self._log_dir) if self._log_dir else None
        if log_dir is not None and log_dir.exists():
            for f in log_dir.glob("SESSION-*.md"):
                m = re.match(r"SESSION-(\d+)\.md", f.name)
                if m:
                    last_num = max(last_num, int(m.group(1)))
        if log_dir is None:
            # No on-disk log dir (parser-only / in-memory) — nothing to collide on.
            self._session_counter = last_num + 1
            return f"SESSION-{self._session_counter:03d}"
        log_dir.mkdir(parents=True, exist_ok=True)
        n = last_num + 1
        while True:
            candidate = log_dir / f"SESSION-{n:03d}.md"
            try:
                candidate.touch(exist_ok=False)  # O_EXCL: atomic reserve
                break
            except FileExistsError:
                n += 1  # another process/turn took this slot — try the next
        self._session_counter = n
        return f"SESSION-{n:03d}"
