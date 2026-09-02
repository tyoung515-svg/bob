"""
BoBClaw Core — CoCouncil real per-token cost meter (COST-2 / MS5-C1), network-free.

Proves the meter that replaces the P1b ``make_cost_fn()→None`` governor guess:

  * ``council_token_usd`` prices a combined token count via the PROVEN rate table
    (``core.backends._cost.usd_for`` — the same PAYG rates ``test_spend.py`` pins),
    splitting 50/50 input/output; prefers real provider ``usage`` when present;
    0/None/negative ⇒ 0.0.
  * ``make_cost_fn`` returns a REAL callable (no longer None) whose figure equals
    ``council_token_usd`` and, injected into ``CouncilEngine``, produces a non-zero
    per-session cost.
  * INTEGRATION — a FUSION council round (panel seats + synth) accrues a NON-ZERO
    ``council_cost_usd`` that EQUALS the rate-table computation for the mocked seats
    (not a magic number), accumulates onto any prior cost, and is SKIPPED in debate
    mode (debate_converge owns that round's estimate — no double-count).
  * INTEGRATION — the SEQUENTIAL ``council_node`` accrues a non-zero
    ``council_cost_usd`` equal to an independently-run engine's metered estimate.

ESTIMATION BASIS (honest): every figure is a POST-HOC TEXT-DERIVED ESTIMATE — the
send seam drops provider ``usage`` (COST-1, out of scope). See panel.council_token_usd.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.backends import _cost
from core.nodes.budget_runtime import measure_spend
from core.nodes.panel import (
    _COUNCIL_INPUT_FRACTION,
    _COUNCIL_SYSTEM_BASE,
    council_token_usd,
    make_cost_fn,
    panel_worker_node,
)

# A valid COUNCIL HANDOFF block the engine's parser round-trips.
_HANDOFF_OUTPUT = """\
The council reconciles the panel: option A is the strongest.

### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** IDEA-01
- **[ACTIVE DEBATE]:** None
- **[BLOCKED]:** None
- **[CORRECTION]:** None
- **[NEXT TASK]:** @Human: proceed
"""


# ─── council_token_usd: rate math against the proven table ───────────────────

def test_council_token_usd_explicit_rate_math():
    """1M combined tokens, split 50/50, priced by usd_for: input $0.95/M +
    output $4.00/M → 0.5*0.95 + 0.5*4.00 = $2.475."""
    got = council_token_usd(1_000_000)
    expected = _cost.usd_for(input_tokens=500_000, output_tokens=500_000)
    assert got == pytest.approx(expected)
    assert got == pytest.approx(2.475)  # 0.475 + 2.00


def test_council_token_usd_split_fraction_is_half():
    assert _COUNCIL_INPUT_FRACTION == 0.5


def test_council_token_usd_matches_usd_for_for_arbitrary_count():
    for t in (1, 7, 100, 3333, 250_001):
        in_t = round(t * _COUNCIL_INPUT_FRACTION)
        expected = _cost.usd_for(input_tokens=in_t, output_tokens=t - in_t)
        assert council_token_usd(t) == pytest.approx(expected)


def test_council_token_usd_zero_and_negative_and_none_are_free():
    assert council_token_usd(0) == 0.0
    assert council_token_usd(-5) == 0.0
    assert council_token_usd(None) == 0.0  # type: ignore[arg-type]


def test_council_token_usd_prefers_real_usage_when_present():
    """Real provider usage (the input/output split) is priced verbatim, ignoring
    the combined ``tokens`` arg — proving the 'thread real usage where available'
    branch (COST-1: not on the current seam, but wired for when it is)."""
    # All input.
    assert council_token_usd(0, usage={"prompt_tokens": 1_000_000}) == pytest.approx(0.95)
    # All output.
    assert council_token_usd(0, usage={"completion_tokens": 1_000_000}) == pytest.approx(4.00)
    # Cached input is cheaper (prompt includes cached; parse_usage subtracts it).
    got = council_token_usd(0, usage={"prompt_tokens": 1_000_000,
                                      "cached_tokens": 1_000_000,
                                      "completion_tokens": 0})
    assert got == pytest.approx(0.16)  # all input was a cache hit
    # usage wins over the tokens arg (would be $2.475 if it fell back).
    assert council_token_usd(1_000_000, usage={"completion_tokens": 1_000_000}) == pytest.approx(4.00)


# ─── make_cost_fn: real callable, rate-consistent, name-independent ──────────

def test_make_cost_fn_is_a_real_callable_not_none():
    fn = make_cost_fn()
    assert fn is not None and callable(fn)


def test_make_cost_fn_prices_against_the_rate_table_and_ignores_name():
    fn = make_cost_fn()
    for t in (0, 1, 5000, 1_000_000):
        # Priced via the rate table (usd_for over the 50/50 split — same computation
        # council_token_usd performs; the independent pins are the CONCRETE 2.475
        # below + the name-independence check).
        in_t = round(t * _COUNCIL_INPUT_FRACTION)
        assert fn("claude-opus-4-6", t) == pytest.approx(
            _cost.usd_for(input_tokens=in_t, output_tokens=t - in_t))
        # A single reference rate is applied regardless of vendor name (COST-3) — a
        # genuine behavioural check, not tautological.
        assert fn("gemini-2.0-flash", t) == pytest.approx(fn("local", t))
    # Concrete hand-computed value: 1M tokens → 0.5M in @ $0.95/M + 0.5M out @ $4/M.
    assert fn("anything", 1_000_000) == pytest.approx(2.475)


async def test_make_cost_fn_injected_into_engine_yields_nonzero_cost(tmp_path):
    """Injected into a real CouncilEngine, make_cost_fn produces a non-zero,
    rate-consistent per-session cost (was 0.0 with the P1b None hook)."""
    from core.council.engine import CouncilEngine

    async def _mock(system, msg):
        return "a council voice reply with enough words to be some real tokens"

    engine = CouncilEngine(
        claude_backend=_mock, gemini_backend=_mock, local_backend=_mock,
        log_dir=str(tmp_path), cost_fn=make_cost_fn(),
    )
    session = await engine.run_session("should we adopt X?")
    assert session.total_cost_estimate > 0.0
    # Pin the total DIRECTLY to the rate table (_cost.usd_for over each voice's own
    # token count, split 50/50) — not via council_token_usd — so the check is not
    # circular with the injected hook. Proves the engine SUMS per-voice correctly.
    def _rate(t: int) -> float:
        in_t = round(t * _COUNCIL_INPUT_FRACTION)
        return _cost.usd_for(input_tokens=in_t, output_tokens=t - in_t)
    expected = round(sum(_rate(v.tokens_used) for v in session.voices), 6)
    assert session.total_cost_estimate == pytest.approx(expected)
    assert len(session.voices) == 3  # the sum spans all three voices, not one call


# ─── INTEGRATION: fusion round accrues non-zero rate-consistent council_cost_usd ─

def _seat_sub(posture, backend, idx):
    return {"seat_posture": posture, "backend": backend, "fallback_chain": [],
            "task": "SHARED BLIND PANEL PROMPT for every seat", "seat_idx": idx,
            "messages": []}


async def _run_panel(subs):
    """Run each seat through the REAL panel_worker (mocked backend) and collect
    panel_results (each entry carrying its metered cost_usd)."""
    async def _panel_send(messages, backend):
        return f"{backend} voice: a substantive council position with real words"

    results = []
    for sub in subs:
        with patch("core.nodes.panel._send_to_backend", _panel_send):
            out = await panel_worker_node(sub)
        results.extend(out["panel_results"])
    return results


async def test_fusion_round_accrues_nonzero_rate_consistent_cost(monkeypatch):
    """The required §5 live-E2E gate (network-free, mocked seats): a fusion council
    round accumulates a NON-ZERO council_cost_usd EQUAL to the rate-table
    computation over the mocked seats + synth — not a hardcoded number."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)

    panel_results = await _run_panel([
        _seat_sub("framer", "claude_api", 0),
        _seat_sub("stress", "gemini_flash", 1),
        _seat_sub("wildcard", "deepseek_v4_flash", 2),
    ])
    # Every seat priced a real, non-zero cost.
    assert all(e["cost_usd"] > 0.0 and e["tokens"] > 0 for e in panel_results)

    captured: dict = {}

    async def _synth_send(messages, backend):
        captured["messages"] = messages
        return "SYNTH reconciled answer" + _HANDOFF_OUTPUT

    state = {
        "task": "the deliberation topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "council_cost_usd": 0.0,
        "panel_results": panel_results,
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend", _synth_send), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await synthesize_node(state)

    # Recompute the EXPECTED figure from the rate table (the same primitives the
    # node uses): sum of the seats' cost_usd + the synth call's metered cost.
    expected_panel = sum(e["cost_usd"] for e in panel_results)
    synth_answer = out["messages"][0]["content"]
    expected_synth = council_token_usd(measure_spend(captured["messages"], synth_answer, None))
    expected = round(0.0 + expected_panel + expected_synth, 6)

    assert out["council_cost_usd"] == pytest.approx(expected)
    assert out["council_cost_usd"] > 0.0        # NON-ZERO
    assert expected_panel > 0.0 and expected_synth > 0.0


async def test_fusion_cost_accumulates_onto_prior_without_double_count(monkeypatch):
    """A grounded-restart round: synthesize adds ONLY the latest round's panel +
    synth onto the running prior (earlier rounds + grounding spawn already in
    prior), never re-charging earlier rounds."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)

    panel_results = await _run_panel([_seat_sub("framer", "claude_api", 0)])
    captured: dict = {}

    async def _synth_send(messages, backend):
        captured["messages"] = messages
        return "answer" + _HANDOFF_OUTPUT

    prior = 0.5  # e.g. round-1 panel/synth ($0.01) + a grounding spawn ($0.25)+…
    state = {
        "task": "t",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "council_cost_usd": prior,
        "panel_results": panel_results,
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend", _synth_send), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await synthesize_node(state)

    expected_panel = sum(e["cost_usd"] for e in panel_results)
    expected_synth = council_token_usd(
        measure_spend(captured["messages"], out["messages"][0]["content"], None))
    assert out["council_cost_usd"] == pytest.approx(round(prior + expected_panel + expected_synth, 6))
    assert out["council_cost_usd"] > prior      # accrued on top of the prior


async def test_fusion_charges_only_latest_round_not_all_panel_results(monkeypatch):
    """Regression for the audit-r1 double-count concern: panel_results is
    operator.add-accumulated, so after a grounded restart it holds BOTH round-0 and
    round-1 seats. synthesize must charge ONLY the latest round's cost_usd (it reads
    the max-round filter), NOT the sum of ALL rounds — else a superseded round would
    re-charge on top of prior_cost."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    captured: dict = {}

    async def _synth_send(messages, backend):
        captured["messages"] = messages
        return "answer" + _HANDOFF_OUTPUT

    # A drifted round-0 seat (expensive) + the superseding round-1 seat (cheap).
    old_seat = {"idx": 0, "posture": "framer", "backend": "claude_api",
                "text": "OLD ROUND0", "round": 0, "tokens": 999_999, "cost_usd": 9.99}
    new_seat = {"idx": 0, "posture": "framer", "backend": "claude_api",
                "text": "NEW ROUND1", "round": 1, "tokens": 40, "cost_usd": 0.0001}
    state = {
        "task": "t",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "council_cost_usd": 0.0,
        "panel_results": [old_seat, new_seat],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend", _synth_send), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await synthesize_node(state)

    expected_synth = council_token_usd(
        measure_spend(captured["messages"], out["messages"][0]["content"], None))
    # Only the round-1 seat (0.0001) is charged, NOT the round-0 seat (9.99).
    assert out["council_cost_usd"] == pytest.approx(round(0.0001 + expected_synth, 6))
    assert out["council_cost_usd"] < 1.0        # the $9.99 stale round was NOT charged


async def test_debate_mode_synthesize_leaves_cost_but_fusion_charges(monkeypatch):
    """No double-count + a positive control (not trivially-green): the SAME
    synthesize_node, same seat, charges council_cost_usd in FUSION mode but NOT in
    DEBATE mode (debate_converge_node owns DEBATE_ROUND_USD — charging here too would
    double-count the round). The fusion arm proves the key isn't simply never-written."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)

    def _state(mode):
        return {
            "task": "t",
            "council_spec": {"mode": mode, "synth_backend": "minimax"},
            "council_cost_usd": 0.0,
            "council_handoff": None,
            "panel_results": [{"idx": 0, "posture": "framer", "backend": "x",
                               "text": "v", "round": 0, "tokens": 999, "cost_usd": 0.01}],
            "messages": [{"role": "user", "content": "q"}],
            "council_pending_answer": None,
        }

    with patch("core.nodes.synthesize._send_to_backend", AsyncMock(return_value="ans")), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        debate_out = await synthesize_node(_state("debate"))
        fusion_out = await synthesize_node(_state("fusion"))

    # DEBATE: synthesize leaves the meter to debate_converge (prior 0.0 unchanged).
    assert "council_cost_usd" not in debate_out
    # FUSION positive control: the SAME node DOES charge the seat's cost_usd (0.01)
    # plus the synth cost — so the debate omission is a real branch, not never-written.
    assert fusion_out["council_cost_usd"] >= 0.01
    assert fusion_out["council_cost_usd"] > 0.0


# ─── INTEGRATION: sequential council_node accrues rate-consistent cost ───────

async def test_sequential_council_node_accrues_rate_consistent_cost(tmp_path):
    """council_node (sequential) now reports a non-zero council_cost_usd equal to
    an independently-constructed engine's metered estimate over the SAME mocked
    backends — rate-consistent, and > 0 (was silently 0/absent in P1b)."""
    from core.council.engine import CouncilEngine
    from core.nodes import council as council_mod
    from core.nodes.panel import make_backend_fn, resolve_seat_backend

    async def _mock(messages, backend):
        # Deterministic per-backend reply → deterministic token counts.
        if backend == "minimax":            # synth voice returns the handoff
            return "synth: " + _HANDOFF_OUTPUT
        return f"{backend} voice: a real council position with several words"

    state = {
        "task": "decide the architecture",
        "council_spec": {"mode": "sequential", "synth_backend": "minimax"},
        "council_cost_usd": 0.0,
        "messages": [],
    }
    with patch("core.nodes.panel._send_to_backend", _mock), \
         patch("core.nodes.council._append_agent_turn_event", AsyncMock()), \
         patch.object(council_mod.CouncilEngine, "save_session_log", lambda self, s: ""):
        out = await council_mod.council_node(state)

    assert out["council_cost_usd"] > 0.0

    # Independently reconstruct the engine council_node builds, run it over the same
    # mocked backends, and confirm the metered figure matches (not a magic number).
    framer_backend, _, _ = resolve_seat_backend("framer")
    stress_backend, _, _ = resolve_seat_backend("stress")
    synth_backend = "minimax"
    engine = CouncilEngine(
        claude_backend=make_backend_fn(framer_backend),
        gemini_backend=make_backend_fn(stress_backend),
        local_backend=make_backend_fn(synth_backend),
        cost_fn=make_cost_fn(),
        log_dir=str(tmp_path),
    )
    with patch("core.nodes.panel._send_to_backend", _mock):
        session = await engine.run_session("decide the architecture")
    assert out["council_cost_usd"] == pytest.approx(round(session.total_cost_estimate, 6))
