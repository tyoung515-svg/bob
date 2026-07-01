"""
Tests for core/council/engine.py — CoCouncil P1a (ported from JackBot Phase 3D).

All model backends are mocked async callables — no API calls, no network.
Cost metering is exercised via injected ``cost_fn`` hooks (the engine has no
hardcoded price map; default ``cost_fn=None`` yields 0.0).
"""

import pytest
from pathlib import Path

from core.council.engine import (
    CouncilEngine,
    CouncilSession,
    CouncilVoice,
    CouncilHandoff,
    _HANDOFF_TEMPLATE,
    _PROTOCOLS_SUMMARY_TEMPLATE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_PROTOCOL_MD = """\
# Council OS — Version 1.0

## 1. Communication Protocols

### `[PROT-01]` Delta-Only Messaging
*Proposed by: Gemini.*

No summaries — only new content.

### `[PROT-02]` Direct Citation & Traceability
*Proposed by: Gemini + Claude.*

Quote the specific text you are engaging with.

### `[PROT-03]` Falsifiable Prompts Over Confidence Claims
*Proposed by: Gemini + Local.*

State load-bearing assumptions, not confidence levels.

## 3. Dynamic Roles

### `[ROLE-01]` The Designated Synthesizer
*Proposed by: Gemini + Local.*

Final voice: resolve IDs, produce HANDOFF block.

### `[ROLE-02]` Assumption Stress-Testing
*Proposed by: Gemini.*

Hunt structural weaknesses in prior claims.

## 5. The Council Handoff Block

### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** (List Idea IDs closed this round)
- **[ACTIVE DEBATE]:** (List Idea IDs currently being stress-tested)
- **[BLOCKED]:** (What we need from human to proceed)
- **[CORRECTION]:** (Any hallucination or error flags)
- **[NEXT TASK]:** (@NextVoice or @Human: specific directive)
"""

SAMPLE_SYNTHESIS_WITH_HANDOFF = """\
Based on both voices, the key decision points are clear.

Claude's assumption about incremental delivery is sound.
Gemini's stress-test of the timeline is valid.

### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** [P-01], [PROT-01]
- **[ACTIVE DEBATE]:** [Q-01], [Q-03]
- **[BLOCKED]:** Human decision on session memory location
- **[CORRECTION]:** None
- **[NEXT TASK]:** @Human: Please ratify the session memory document location
"""


# A flat per-token cost hook for cost tests: claude is expensive, local is free.
# This replaces the engine's old hardcoded _COST_PER_TOKEN price map — the
# *shape* of the differential is what we assert, not specific dollar figures.
def _flat_cost_fn(model: str, tokens: int) -> float:
    rate = {
        "claude-opus-4-6": 45e-6,
        "gemini-2.0-flash": 0.19e-6,
        "local": 0.0,
    }.get(model, 45e-6)
    return tokens * rate


def _make_engine(tmp_path=None, local=False, cost_fn=None):
    """Create a CouncilEngine with async mock backends (no network)."""
    calls = {"claude": [], "gemini": [], "local": []}

    async def mock_claude(system: str, msg: str) -> str:
        calls["claude"].append({"system": system, "msg": msg})
        return "Claude voice response: initial analysis with [PROT-01] compliance."

    async def mock_gemini(system: str, msg: str) -> str:
        calls["gemini"].append({"system": system, "msg": msg})
        return "Gemini voice response: stress-testing Claude's assumptions per [ROLE-02]."

    async def mock_local(system: str, msg: str) -> str:
        calls["local"].append({"system": system, "msg": msg})
        return SAMPLE_SYNTHESIS_WITH_HANDOFF

    engine = CouncilEngine(
        claude_backend=mock_claude,
        gemini_backend=mock_gemini,
        local_backend=mock_local if local else None,
        log_dir=str(tmp_path) if tmp_path else "",
        cost_fn=cost_fn,
    )
    engine._calls = calls
    return engine


# ---------------------------------------------------------------------------
# Tests: load_protocols
# ---------------------------------------------------------------------------

class TestLoadProtocols:
    def test_load_protocols_parses_ids(self, tmp_path):
        proto_file = tmp_path / "COUNCIL-OS-v1.0.md"
        proto_file.write_text(SAMPLE_PROTOCOL_MD, encoding="utf-8")

        engine = _make_engine(tmp_path)
        result = engine.load_protocols(str(proto_file))

        assert "[PROT-01]" in result["ids"]
        assert "[PROT-02]" in result["ids"]
        assert "[ROLE-01]" in result["ids"]
        assert "[ROLE-02]" in result["ids"]

    def test_load_protocols_summary_contains_ids(self, tmp_path):
        proto_file = tmp_path / "COUNCIL-OS-v1.0.md"
        proto_file.write_text(SAMPLE_PROTOCOL_MD, encoding="utf-8")

        engine = _make_engine(tmp_path)
        result = engine.load_protocols(str(proto_file))

        assert "[PROT-01]" in result["summary"]
        assert "[ROLE-01]" in result["summary"]

    def test_load_protocols_caches_after_first_load(self, tmp_path):
        proto_file = tmp_path / "COUNCIL-OS-v1.0.md"
        proto_file.write_text(SAMPLE_PROTOCOL_MD, encoding="utf-8")

        engine = _make_engine(tmp_path)
        result1 = engine.load_protocols(str(proto_file))
        # Delete the file — second call should return cached result
        proto_file.unlink()
        result2 = engine.load_protocols(str(proto_file))

        assert result1 is result2  # same object (cached)

    def test_load_protocols_missing_file_uses_builtin_summary(self):
        engine = _make_engine()
        result = engine.load_protocols("/nonexistent/path/COUNCIL-OS.md")

        assert result["summary"] == _PROTOCOLS_SUMMARY_TEMPLATE
        assert "[PROT-01]" in result["summary"]

    def test_load_protocols_preserves_full_text(self, tmp_path):
        proto_file = tmp_path / "COUNCIL-OS-v1.0.md"
        proto_file.write_text(SAMPLE_PROTOCOL_MD, encoding="utf-8")

        engine = _make_engine(tmp_path)
        result = engine.load_protocols(str(proto_file))

        assert "Council OS" in result["full_text"]

    def test_load_protocols_default_path_resolves_in_tree_doc(self):
        """With no explicit path, the engine finds the shipped COUNCIL-OS doc."""
        engine = _make_engine()
        result = engine.load_protocols()

        # The verbatim in-tree protocol doc parses its real tagged IDs.
        assert "[PROT-01]" in result["ids"]
        assert "[ROLE-01]" in result["ids"]
        assert "[P-01]" in result["ids"]
        assert "Council OS" in result["full_text"]


# ---------------------------------------------------------------------------
# Tests: run_session — voice ordering and prompt content
# ---------------------------------------------------------------------------

class TestRunSession:
    async def test_run_session_calls_all_three_voices(self, tmp_path):
        """All three backends are called in order."""
        engine = _make_engine(tmp_path, local=True)
        await engine.run_session("Should we adopt event-driven architecture?")

        assert len(engine._calls["claude"]) == 1
        assert len(engine._calls["gemini"]) == 1
        assert len(engine._calls["local"]) == 1

    async def test_claude_voice_prompt_includes_protocols(self, tmp_path):
        """The message passed to the Claude backend includes protocol instructions."""
        engine = _make_engine(tmp_path, local=True)
        await engine.run_session("Microservices vs monolith")

        claude_msg = engine._calls["claude"][0]["msg"]
        assert "PROT-01" in claude_msg
        assert "PROTOCOLS IN EFFECT" in claude_msg

    async def test_claude_voice_prompt_includes_topic(self, tmp_path):
        """The Claude prompt includes the topic."""
        engine = _make_engine(tmp_path, local=True)
        topic = "Database migration strategy"
        await engine.run_session(topic)

        claude_msg = engine._calls["claude"][0]["msg"]
        assert topic in claude_msg

    async def test_claude_voice_prompt_includes_prior_context(self, tmp_path):
        """Prior context is injected into the Claude voice prompt."""
        engine = _make_engine(tmp_path, local=True)
        await engine.run_session("New topic", context="Prior: we chose PostgreSQL.")

        claude_msg = engine._calls["claude"][0]["msg"]
        assert "Prior: we chose PostgreSQL." in claude_msg

    async def test_gemini_voice_prompt_includes_claude_response(self, tmp_path):
        """Claude's response is passed to the Gemini backend."""
        engine = _make_engine(tmp_path, local=True)
        await engine.run_session("API gateway options")

        gemini_msg = engine._calls["gemini"][0]["msg"]
        assert "Claude voice response" in gemini_msg

    async def test_synthesis_prompt_includes_both_voices(self, tmp_path):
        """Both Claude and Gemini responses appear in the synthesis prompt."""
        engine = _make_engine(tmp_path, local=True)
        await engine.run_session("CI/CD pipeline design")

        synth_msg = engine._calls["local"][0]["msg"]
        assert "Claude voice response" in synth_msg
        assert "Gemini voice response" in synth_msg

    async def test_run_session_returns_council_session(self, tmp_path):
        """run_session returns a CouncilSession dataclass."""
        engine = _make_engine(tmp_path, local=True)
        session = await engine.run_session("test topic")

        assert isinstance(session, CouncilSession)
        assert session.topic == "test topic"
        assert len(session.voices) == 3

    async def test_run_session_voice_roles_assigned(self, tmp_path):
        """Voices are assigned roles: claude, gemini, synthesizer (in order)."""
        engine = _make_engine(tmp_path, local=True)
        session = await engine.run_session("role check")

        roles = [v.role for v in session.voices]
        assert roles == ["claude", "gemini", "synthesizer"]

    async def test_run_session_protocols_applied_populated(self, tmp_path):
        """Session records the protocols applied."""
        engine = _make_engine(tmp_path, local=True)
        session = await engine.run_session("protocol check")

        assert "COUNCIL-OS-v1.0" in session.protocols_applied
        assert "PROT-01" in session.protocols_applied

    async def test_no_local_model_falls_back_to_claude(self, tmp_path):
        """When local=None, synthesis calls Claude backend (2 claude calls total)."""
        engine = _make_engine(tmp_path, local=False)
        session = await engine.run_session("fallback test")

        assert len(engine._calls["claude"]) == 2  # voice + synthesis
        assert len(engine._calls["local"]) == 0
        synth_voice = next(v for v in session.voices if v.role == "synthesizer")
        assert synth_voice.model == "claude-opus-4-6"

    async def test_run_session_total_tokens_positive(self, tmp_path):
        """Total token count is positive."""
        engine = _make_engine(tmp_path, local=True)
        session = await engine.run_session("token count test")

        assert session.total_tokens > 0
        assert session.total_tokens == sum(v.tokens_used for v in session.voices)


# ---------------------------------------------------------------------------
# Tests: extract_handoff
# ---------------------------------------------------------------------------

class TestExtractHandoff:
    def test_extract_handoff_parses_correctly(self):
        engine = _make_engine()
        handoff = engine._extract_handoff(SAMPLE_SYNTHESIS_WITH_HANDOFF)

        assert "[P-01]" in handoff.resolved
        assert "[PROT-01]" in handoff.resolved
        assert "[Q-01]" in handoff.active_debate
        assert "session memory" in handoff.blocked[0].lower()
        assert "ratify" in handoff.next_task.lower()

    def test_extract_handoff_handles_missing_block(self):
        """No COUNCIL HANDOFF block → empty CouncilHandoff, no crash."""
        engine = _make_engine()
        handoff = engine._extract_handoff("This synthesis has no handoff block at all.")

        assert handoff.resolved == []
        assert handoff.active_debate == []
        assert handoff.blocked == []
        assert handoff.corrections == []
        assert handoff.next_task == ""

    def test_extract_handoff_handles_none_fields(self):
        """Fields containing 'None' are returned as empty lists."""
        synthesis = """\
### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** None
- **[ACTIVE DEBATE]:** None
- **[BLOCKED]:** None
- **[CORRECTION]:** None
- **[NEXT TASK]:** @Human: proceed
"""
        engine = _make_engine()
        handoff = engine._extract_handoff(synthesis)

        assert handoff.resolved == []
        assert handoff.active_debate == []
        assert handoff.blocked == []
        assert handoff.corrections == []
        assert handoff.next_task == "@Human: proceed"

    def test_extract_handoff_multi_item_resolved(self):
        """Multiple comma-separated IDs in RESOLVED are split correctly."""
        synthesis = """\
### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** [P-01], [PROT-01], [PROT-02]
- **[ACTIVE DEBATE]:** [Q-01]
- **[BLOCKED]:** Nothing
- **[CORRECTION]:** None
- **[NEXT TASK]:** Continue
"""
        engine = _make_engine()
        handoff = engine._extract_handoff(synthesis)

        assert len(handoff.resolved) == 3
        assert "[P-01]" in handoff.resolved
        assert "[PROT-02]" in handoff.resolved


# ---------------------------------------------------------------------------
# Tests: save_session_log
# ---------------------------------------------------------------------------

class TestSaveSessionLog:
    def _make_session(self) -> CouncilSession:
        return CouncilSession(
            session_id="SESSION-042",
            topic="Test topic for log",
            voices=[
                CouncilVoice("claude-opus-4-6", "claude", "Claude says X.", 100, 500),
                CouncilVoice("gemini-2.0-flash", "gemini", "Gemini counters Y.", 80, 300),
                CouncilVoice("local", "synthesizer", SAMPLE_SYNTHESIS_WITH_HANDOFF, 120, 200),
            ],
            synthesis=SAMPLE_SYNTHESIS_WITH_HANDOFF,
            handoff=CouncilHandoff(
                resolved=["[P-01]"],
                active_debate=["[Q-01]"],
                blocked=["Human decision needed"],
                corrections=[],
                next_task="@Human: ratify",
            ),
            protocols_applied=["COUNCIL-OS-v1.0", "PROT-01"],
            timestamp="2026-04-03T12:00:00+00:00",
            total_tokens=300,
            total_cost_estimate=0.0135,
        )

    def test_save_session_log_creates_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        session = self._make_session()
        log_path = engine.save_session_log(session, str(tmp_path))

        assert Path(log_path).exists()
        assert Path(log_path).name == "SESSION-042.md"

    def test_save_session_log_contains_topic(self, tmp_path):
        engine = _make_engine(tmp_path)
        session = self._make_session()
        log_path = engine.save_session_log(session, str(tmp_path))

        content = Path(log_path).read_text(encoding="utf-8")
        assert "Test topic for log" in content

    def test_save_session_log_contains_all_voices(self, tmp_path):
        engine = _make_engine(tmp_path)
        session = self._make_session()
        log_path = engine.save_session_log(session, str(tmp_path))

        content = Path(log_path).read_text(encoding="utf-8")
        assert "## Claude Voice" in content
        assert "## Gemini Voice" in content
        assert "## Synthesis" in content
        assert "Claude says X." in content
        assert "Gemini counters Y." in content

    def test_save_session_log_contains_handoff_section(self, tmp_path):
        engine = _make_engine(tmp_path)
        session = self._make_session()
        log_path = engine.save_session_log(session, str(tmp_path))

        content = Path(log_path).read_text(encoding="utf-8")
        assert "## Council Handoff" in content
        assert "[RESOLVED]" in content
        assert "[P-01]" in content
        assert "[NEXT TASK]" in content

    def test_save_session_log_contains_metrics(self, tmp_path):
        engine = _make_engine(tmp_path)
        session = self._make_session()
        log_path = engine.save_session_log(session, str(tmp_path))

        content = Path(log_path).read_text(encoding="utf-8")
        assert "## Metrics" in content
        assert "300" in content        # total_tokens
        assert "0.013500" in content   # cost (dataclass field, formatted to 6dp)

    def test_save_session_log_defaults_to_engine_log_dir(self, tmp_path):
        """Omitting log_dir writes into the engine's configured log dir."""
        engine = _make_engine(tmp_path)
        session = self._make_session()
        log_path = engine.save_session_log(session)  # no explicit dir

        assert Path(log_path).exists()
        assert Path(log_path).parent == Path(str(tmp_path))


# ---------------------------------------------------------------------------
# Tests: session ID generation
# ---------------------------------------------------------------------------

class TestSessionId:
    def test_session_id_starts_at_001_when_no_logs(self, tmp_path):
        engine = _make_engine(tmp_path)
        sid = engine._generate_session_id()
        assert sid == "SESSION-001"

    def test_session_id_increments_from_last_logged(self, tmp_path):
        # Create existing session files
        (tmp_path / "SESSION-001.md").write_text("# s1")
        (tmp_path / "SESSION-007.md").write_text("# s7")

        engine = _make_engine(tmp_path)
        sid = engine._generate_session_id()
        assert sid == "SESSION-008"

    def test_session_id_pads_to_three_digits(self, tmp_path):
        (tmp_path / "SESSION-009.md").write_text("# s")
        engine = _make_engine(tmp_path)
        sid = engine._generate_session_id()
        assert sid == "SESSION-010"

    def test_session_id_ignores_non_session_files(self, tmp_path):
        (tmp_path / "CHECKPOINT-001.md").write_text("# checkpoint")
        (tmp_path / "README.md").write_text("# readme")
        engine = _make_engine(tmp_path)
        sid = engine._generate_session_id()
        assert sid == "SESSION-001"

    async def test_session_id_assigned_to_session_object(self, tmp_path):
        engine = _make_engine(tmp_path, local=True)
        session = await engine.run_session("id test")
        assert session.session_id.startswith("SESSION-")
        assert len(session.session_id) == len("SESSION-001")

    def test_session_id_reserves_slot_to_avoid_collision(self, tmp_path):
        """Multi-process safety: _generate_session_id RESERVES its slot on disk
        (O_EXCL placeholder), so a concurrent caller globbing the same dir picks
        the NEXT id instead of colliding on one SESSION-NNN file (silent overwrite)."""
        engine = _make_engine(tmp_path)
        sid1 = engine._generate_session_id()
        assert sid1 == "SESSION-001"
        assert (tmp_path / "SESSION-001.md").exists()  # slot reserved on disk

        # A second engine (a separate "process") sees the reservation, not a collision.
        engine2 = _make_engine(tmp_path)
        sid2 = engine2._generate_session_id()
        assert sid2 == "SESSION-002"
        assert sid1 != sid2


# ---------------------------------------------------------------------------
# Tests: cost estimate (pluggable cost_fn hook — no hardcoded price map)
# ---------------------------------------------------------------------------

class TestCostEstimate:
    async def test_default_no_cost_fn_yields_zero(self, tmp_path):
        """With no cost_fn injected, per-session cost is 0.0 (metering skipped)."""
        engine = _make_engine(tmp_path, local=False)  # cost_fn=None by default
        session = await engine.run_session("no cost fn")

        assert session.total_cost_estimate == 0.0

    async def test_injected_cost_fn_is_used(self, tmp_path):
        """An injected cost_fn produces a positive, sane cost estimate."""
        engine = _make_engine(tmp_path, local=False, cost_fn=_flat_cost_fn)
        session = await engine.run_session("cost estimate test")

        assert session.total_cost_estimate > 0
        assert session.total_cost_estimate < 1.0  # sanity ceiling

    async def test_cost_fn_called_per_voice(self, tmp_path):
        """cost_fn receives each voice's (model, tokens) pair."""
        seen = []

        def spy_cost_fn(model: str, tokens: int) -> float:
            seen.append((model, tokens))
            return 0.0

        engine = _make_engine(tmp_path, local=True, cost_fn=spy_cost_fn)
        session = await engine.run_session("spy test")

        assert len(seen) == len(session.voices) == 3
        models = [m for m, _ in seen]
        assert "claude-opus-4-6" in models
        assert "gemini-2.0-flash" in models
        assert "local" in models

    async def test_local_synthesis_reduces_cost(self, tmp_path):
        """With a cost_fn, local synthesis costs less than all-Claude synthesis."""
        engine_local = _make_engine(tmp_path, local=True, cost_fn=_flat_cost_fn)
        session_local = await engine_local.run_session("local synthesis test")

        engine_claude = _make_engine(tmp_path, local=False, cost_fn=_flat_cost_fn)
        session_claude = await engine_claude.run_session("claude synthesis test")

        assert session_local.total_cost_estimate < session_claude.total_cost_estimate

    async def test_cost_fn_failure_is_fail_soft(self, tmp_path):
        """A raising cost_fn must not abort a completed session (cost → 0.0)."""
        def boom(model: str, tokens: int) -> float:
            raise RuntimeError("metering backend down")

        engine = _make_engine(tmp_path, local=True, cost_fn=boom)
        session = await engine.run_session("fail-soft cost")

        assert session.total_cost_estimate == 0.0
        assert len(session.voices) == 3  # session still completed
