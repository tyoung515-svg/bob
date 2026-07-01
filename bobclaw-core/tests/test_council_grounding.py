"""
BoBClaw Core — CoCouncil P2 grounding-gate tests (network-free).

Covers (per the P2 brief + design §A2/A3/A4):
  * verdict parse: tolerant of bare / fenced / malformed JSON; a parse failure
    fails OPEN → converge.
  * drift formula (OPEN-B = ratio): contradicted ratio ≥ threshold → restart;
    below → converge; all-unverifiable → converge + flagged (no restart).
  * grounded restart: drift + budget remaining → grounding_node increments
    council_restart AND writes the re-seed context (OG+output+steer+research),
    and that context reaches _build_panel_task; _route_after_ground →
    panel_dispatch.
  * restart-budget exhaustion: council_restart == COUNCIL_RESTART_BUDGET →
    converge (no more restarts), best handoff kept.
  * cost ceiling: simulated breach → fail-loud END with best handoff.
  * topology (P3b): the `ground` node is ALWAYS wired (synthesize → ground);
    grounding on/off is a RUNTIME decision (grounding_enabled), not build-time.
  * per-run bounds (P3b): a profile's protocol_bounds override the global
    constants for the run — grounding on/off, max_usd ceiling, restart_budget,
    drift_threshold — falling back to the globals when unset.
  * isolation: a grounding spawn FAILURE (timeout/spawn) fails open → converge;
    the graph still compiles; existing nodes untouched.

The grounding claude_code spawn is mocked by patching
``core.backends.claude_code.ClaudeCodeClient`` (the grounding node imports it
lazily inside the function), so nothing hits the network or spawns a subprocess.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import (
    COUNCIL_DRIFT_THRESHOLD,
    COUNCIL_RESTART_BUDGET,
)
from core.nodes import grounding as grounding_mod
from core.nodes.grounding import (
    _build_reseed_context,
    compute_drift,
    extract_json,
    grounding_node,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _verdict_json(claims, research="findings here"):
    return json.dumps({"claims": claims, "research": research})


def _state(**overrides):
    """A fusion-close state arriving at the grounding gate."""
    base = {
        "task": "Did the Fusion launch beat Fable 5 on cost?",
        "face_id": "council-max",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax",
                         "seats": ["framer", "stress", "wildcard"]},
        "council_restart": 0,
        "council_cost_usd": 0.0,
        "council_handoff": {"resolved": ["IDEA-01"], "active_debate": [],
                            "blocked": [], "corrections": [], "next_task": ""},
        "messages": [
            {"role": "user", "content": "the question"},
            {"role": "assistant", "content": "The council concludes: Fusion is ~half cost."},
        ],
        "conversation_id": "conv-1",
    }
    base.update(overrides)
    return base


def _mock_cc(text):
    """Patch ClaudeCodeClient so .chat returns {"text": text}."""
    client = MagicMock()
    client.chat = AsyncMock(return_value={"text": text, "session_id": "s1",
                                          "is_error": False})
    cls = MagicMock(return_value=client)
    return cls


# ─── verdict parse (mirrors critic.extract_json) ─────────────────────────────

def test_extract_json_bare_object():
    raw = _verdict_json([{"claim": "x", "status": "confirmed", "note": "ok"}])
    data = extract_json(raw)
    assert data and data["claims"][0]["status"] == "confirmed"


def test_extract_json_fenced():
    raw = "```json\n" + _verdict_json([{"claim": "x", "status": "contradicted"}]) + "\n```"
    data = extract_json(raw)
    assert data and data["claims"][0]["status"] == "contradicted"


def test_extract_json_with_prose_prefix():
    raw = ("Here is my verdict after checking the web:\n"
           + _verdict_json([{"claim": "x", "status": "unverifiable"}]))
    data = extract_json(raw)
    assert data and data["claims"][0]["status"] == "unverifiable"


def test_extract_json_malformed_returns_none():
    assert extract_json("not json at all, sorry") is None
    assert extract_json("{ claims: [ broken ") is None


async def test_grounding_parse_failure_fails_open_converge():
    """Unparseable verdict → converge (no restart, council_restart unchanged)."""
    st = _state()
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc("the model rambled and never returned JSON")):
        out = await grounding_node(st)
    assert "council_restart" not in out  # converge leaves restart unchanged
    assert (out.get("council_spec") or {}).get("reseed_context") is None
    assert out["grounding_verdict"]["parse_error"] is True


# ─── drift formula (the documented testable helper) ──────────────────────────

def test_compute_drift_no_claims_no_restart():
    d = compute_drift([])
    assert d["should_restart"] is False
    assert d["drift_ratio"] == 0.0 and d["restart_ratio"] == 0.0


def test_compute_drift_all_confirmed_no_restart():
    claims = [{"claim": "a", "status": "confirmed", "note": ""}] * 3
    d = compute_drift(claims)
    assert d["n_confirmed"] == 3
    assert d["should_restart"] is False


def test_compute_drift_contradicted_over_threshold_restarts():
    # 2 of 3 contradicted = 0.66 ≥ 0.34 default → restart.
    claims = [
        {"claim": "a", "status": "contradicted", "note": ""},
        {"claim": "b", "status": "contradicted", "note": ""},
        {"claim": "c", "status": "confirmed", "note": ""},
    ]
    d = compute_drift(claims)
    assert d["n_contradicted"] == 2
    assert d["restart_ratio"] == pytest.approx(2 / 3)
    assert d["should_restart"] is True


def test_compute_drift_contradicted_below_threshold_converges():
    # 1 of 10 contradicted = 0.10 < 0.34 → converge.
    claims = [{"claim": f"c{i}", "status": "confirmed", "note": ""} for i in range(9)]
    claims.append({"claim": "bad", "status": "contradicted", "note": ""})
    d = compute_drift(claims)
    assert d["n_contradicted"] == 1
    assert d["restart_ratio"] == pytest.approx(0.1)
    assert d["should_restart"] is False


def test_compute_drift_all_unverifiable_no_restart():
    """Unverifiable alone (no contradictions) never forces a restart (§A2)."""
    claims = [{"claim": f"u{i}", "status": "unverifiable", "note": ""} for i in range(5)]
    d = compute_drift(claims)
    assert d["n_unverifiable"] == 5
    assert d["drift_ratio"] == 1.0       # fully "unsupported"
    assert d["restart_ratio"] == 0.0     # but zero contradictions
    assert d["should_restart"] is False


# ─── grounding_node: converge vs restart end-to-end ──────────────────────────

async def test_grounding_all_unverifiable_converges_and_flags():
    """All-unverifiable → converge + the unverifiable claims flag the handoff."""
    claims = [{"claim": "Fusion cost is half", "status": "unverifiable", "note": "no source"}]
    st = _state()
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims))):
        out = await grounding_node(st)
    assert "council_restart" not in out  # converge
    assert (out.get("council_spec") or {}).get("reseed_context") is None
    # The unverifiable claim was appended to the handoff's active_debate.
    flagged = out["council_handoff"]["active_debate"]
    assert any("UNVERIFIED" in f for f in flagged)


async def test_grounding_drift_triggers_restart_with_reseed():
    """Drift + budget remaining → restart: council_restart++ AND reseed context."""
    claims = [
        {"claim": "Fusion beat Fable 5", "status": "contradicted", "note": "web says within 1%, not beat"},
        {"claim": "at half cost", "status": "contradicted", "note": "web says ~half"},
        {"claim": "launched 2026-06-12", "status": "confirmed", "note": "matches"},
    ]
    st = _state(council_restart=0)
    # Seed the prior round's panel artifacts so we can prove the restart clears them.
    st["council_spec"]["resolved_seats"] = [{"idx": 0, "posture": "framer"}]
    st["council_spec"]["panel_task"] = "the drifted round's prompt"
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims, research="Fusion was within ~1%, not a clear beat."))):
        out = await grounding_node(st)
    assert out["council_restart"] == 1
    reseed = out["council_spec"]["reseed_context"]
    assert reseed
    # Re-seed carries OG topic + output-so-far + synth steer + grounding research.
    assert "Fusion launch" in reseed                     # OG topic
    assert "half cost" in reseed                          # output-so-far (prior answer)
    assert "within ~1%" in reseed                         # grounding research
    assert "GROUNDED RESTART" in reseed
    # The drifted round's resolved_seats / panel_task were cleared for the re-run.
    assert "resolved_seats" not in out["council_spec"]
    assert "panel_task" not in out["council_spec"]


async def test_grounding_restart_route_goes_to_panel_dispatch():
    from core.graph import _route_after_ground

    claims = [{"claim": "x", "status": "contradicted", "note": ""},
              {"claim": "y", "status": "contradicted", "note": ""}]
    st = _state(council_restart=0)
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims))):
        out = await grounding_node(st)
    # Apply the delta and route.
    st2 = dict(st)
    st2.update(out)
    assert _route_after_ground(st2) == "panel_dispatch"


async def test_grounding_converge_route_goes_to_end():
    from langgraph.graph import END
    from core.graph import _route_after_ground

    claims = [{"claim": "x", "status": "confirmed", "note": ""}]
    st = _state()
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims))):
        out = await grounding_node(st)
    st2 = dict(st)
    st2.update(out)
    assert _route_after_ground(st2) == END


async def test_reseed_context_reaches_build_panel_task():
    """The re-seed context spliced by grounding reaches the panel prompt."""
    from core.nodes.panel import _build_panel_task, panel_dispatch_node

    claims = [{"claim": "x", "status": "contradicted", "note": ""},
              {"claim": "y", "status": "contradicted", "note": ""}]
    st = _state(council_restart=0)
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims, research="corrected facts"))):
        out = await grounding_node(st)  # noqa: F841 — exercised below via spec
    # The grounding delta carries the re-seed on council_spec; feed it to dispatch.
    st2 = dict(st)
    st2.update(out)
    delta = panel_dispatch_node(st2)
    panel_task = delta["council_spec"]["panel_task"]
    assert "GROUNDED RESTART" in panel_task
    assert "corrected facts" in panel_task
    # And a no-reseed task is byte-identical to P1 (no GROUNDED RESTART block).
    plain = _build_panel_task("topic", "")
    assert "GROUNDED RESTART" not in plain


# ─── budget exhaustion ───────────────────────────────────────────────────────

async def test_grounding_restart_budget_exhausted_converges():
    """council_restart == COUNCIL_RESTART_BUDGET → converge (no more restarts)."""
    claims = [{"claim": "x", "status": "contradicted", "note": ""},
              {"claim": "y", "status": "contradicted", "note": ""}]
    st = _state(council_restart=COUNCIL_RESTART_BUDGET)
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims))):
        out = await grounding_node(st)
    assert "council_restart" not in out  # converge — no increment
    assert (out.get("council_spec") or {}).get("reseed_context") is None
    # Residual drift is flagged for the human in the best handoff.
    flagged = out["council_handoff"]["active_debate"]
    assert any("CONTRADICTED" in f for f in flagged)


# ─── cost ceiling (fail loud) ────────────────────────────────────────────────

async def test_grounding_ceiling_breach_fails_loud():
    """Simulated cost breach → fail-loud END (error + best handoff), no spawn."""
    from core.config import COUNCIL_MAX_USD

    st = _state(council_cost_usd=COUNCIL_MAX_USD + 1.0)
    spawned = {"called": False}

    class _Boom:
        def __init__(self, *a, **k):
            spawned["called"] = True
        async def chat(self, *a, **k):  # pragma: no cover
            raise AssertionError("must not spawn after a ceiling breach")

    with patch("core.backends.claude_code.ClaudeCodeClient", _Boom):
        out = await grounding_node(st)
    assert spawned["called"] is False          # never spawned
    assert out["error"]                         # fail loud
    assert "ceiling" in out["error"].lower()
    # P2 (Refinement 1): the ceiling notice now rides the error frame, NOT
    # `messages`. With no pending answer present, _converge commits no message.
    assert "messages" not in out
    assert "council_restart" not in out         # converge / END


async def test_grounding_ceiling_projected_breach_fails_loud():
    """Cost just under the ceiling such that one more spawn would breach."""
    from core.config import COUNCIL_MAX_USD
    from core.nodes.grounding import GROUNDING_SPAWN_USD

    st = _state(council_cost_usd=COUNCIL_MAX_USD - (GROUNDING_SPAWN_USD / 2))
    with patch("core.backends.claude_code.ClaudeCodeClient", _mock_cc("{}")):
        out = await grounding_node(st)
    assert out["error"] and "ceiling" in out["error"].lower()


# ─── spawn failure fails open ────────────────────────────────────────────────

async def test_grounding_spawn_failure_fails_open_converge():
    """A spawn exception (e.g. timeout) fails OPEN → converge, cost not charged."""
    class _Fail:
        def __init__(self, *a, **k):
            pass
        async def chat(self, *a, **k):
            raise RuntimeError("claude CLI timed out")

    st = _state(council_cost_usd=1.0)
    with patch("core.backends.claude_code.ClaudeCodeClient", _Fail):
        out = await grounding_node(st)
    assert "council_restart" not in out                 # converge
    assert out["grounding_verdict"] is None
    assert out["council_cost_usd"] == 1.0               # failed spawn not charged


async def test_grounding_posture_is_readonly_websearch():
    """The grounding posture maps to plan + WebSearch/WebFetch (read-only)."""
    seen = {}

    class _Spy:
        def __init__(self, *a, **k):
            seen["posture_init"] = k.get("posture")
        async def chat(self, *, prompt, posture=None, **k):
            seen["prompt"] = prompt
            seen["posture_chat"] = posture
            return {"text": _verdict_json([{"claim": "x", "status": "confirmed"}])}

    st = _state()
    with patch("core.backends.claude_code.ClaudeCodeClient", _Spy):
        await grounding_node(st)
    assert seen["posture_chat"]["permission_mode"] == "plan"
    assert seen["posture_chat"]["allowed_tools"] == ["WebSearch", "WebFetch"]
    # The verify prompt carries the answer to be grounded.
    assert "half cost" in seen["prompt"]


# ─── cadence gating in the graph (build-time) ────────────────────────────────

def test_graph_always_wires_ground_node_regardless_of_cadence(monkeypatch):
    """P3b: the `ground` node is ALWAYS wired (the build-time topology gate is
    gone). Grounding on/off is now a RUNTIME decision (grounding_enabled(spec)),
    so even with the global cadence 'off' the node is present — it simply no-op
    converges → END at run time when grounding is disabled for the run."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    from core.graph import build_graph

    graph = build_graph()
    nodes = set(graph.get_graph().nodes.keys())
    assert "ground" in nodes
    assert {"panel_dispatch", "synthesize", "council"}.issubset(nodes)


def test_graph_grounding_on_wires_ground_node(monkeypatch):
    """`ground` node present under the default cadence too (always wired, P3b)."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    from core.graph import build_graph

    graph = build_graph()
    nodes = set(graph.get_graph().nodes.keys())
    assert "ground" in nodes


def test_graph_compiles_with_grounding_on():
    """The full graph still compiles with the P2 grounding wiring present."""
    from core.graph import build_graph

    graph = build_graph()
    assert graph is not None


# ─── reseed builder unit ─────────────────────────────────────────────────────

def test_build_reseed_context_carries_all_four_parts():
    st = _state()
    st["council_handoff"] = {"resolved": [], "active_debate": ["IDEA-03"],
                             "blocked": [], "corrections": [],
                             "next_task": "@Human: confirm pricing"}
    drift = compute_drift([{"claim": "x", "status": "contradicted", "note": ""}])
    reseed = _build_reseed_context(st, "the web says otherwise", drift)
    assert "Fusion launch" in reseed                 # OG topic
    assert "half cost" in reseed                      # output-so-far
    assert "IDEA-03" in reseed                        # synth steer (active debate)
    assert "confirm pricing" in reseed                # synth steer (next task)
    assert "the web says otherwise" in reseed         # grounding research


# ─── P3b: per-run protocol bounds bind ───────────────────────────────────────

def test_grounding_enabled_bounds_override_and_fallback(monkeypatch):
    """grounding_enabled(spec): a profile's protocol_bounds.grounding overrides
    the global cadence in BOTH directions; absent ⇒ fall back to the cadence."""
    from core.nodes.grounding import grounding_enabled

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    # Explicit OFF wins over a global-ON cadence (str + bool forms).
    assert grounding_enabled({"bounds": {"grounding": "off"}}) is False
    assert grounding_enabled({"bounds": {"grounding": False}}) is False
    # No bounds key ⇒ global cadence (ON here).
    assert grounding_enabled({"bounds": {}}) is True
    assert grounding_enabled({}) is True
    assert grounding_enabled(None) is True

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    # Explicit ON wins over a global-OFF cadence.
    assert grounding_enabled({"bounds": {"grounding": "on"}}) is True
    assert grounding_enabled({"bounds": {"grounding": True}}) is True
    # No bounds key ⇒ global cadence (OFF here).
    assert grounding_enabled({"bounds": {}}) is False
    assert grounding_enabled(None) is False


def test_grounding_enabled_coercion_edges(monkeypatch):
    """Hardened coercion (P3b review): numeric falsy = off (int/float agree),
    on/off synonyms tolerated, an UNKNOWN token fails SAFE to off (not silently
    ON), explicit None-as-value ⇒ unset ⇒ cadence fallback."""
    from core.nodes.grounding import grounding_enabled

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    # numeric: 0 and 0.0 both OFF (the old denylist read float 0.0 as ON); nonzero ON.
    assert grounding_enabled({"bounds": {"grounding": 0}}) is False
    assert grounding_enabled({"bounds": {"grounding": 0.0}}) is False
    assert grounding_enabled({"bounds": {"grounding": 1}}) is True
    # off synonyms + case/space tolerance.
    for off in ("OFF", " off ", "false", "no", "none", "disabled", "0"):
        assert grounding_enabled({"bounds": {"grounding": off}}) is False, off
    # on synonyms.
    for on in ("on", "TRUE", "1", "yes", "enabled"):
        assert grounding_enabled({"bounds": {"grounding": on}}) is True, on
    # unknown token fails SAFE to off (cheaper), NOT silently ON.
    assert grounding_enabled({"bounds": {"grounding": "wat"}}) is False
    assert grounding_enabled({"bounds": {"grounding": "preclose"}}) is False
    # explicit None-as-value ⇒ unset ⇒ cadence fallback (ON under preclose).
    assert grounding_enabled({"bounds": {"grounding": None}}) is True


async def test_grounding_ceiling_below_one_spawn_hints_misconfig():
    """A per-run ceiling below one grounding-spawn cost adds a config hint to the
    breach notice (grounding never ran), distinct from a genuine mid-run breach,
    and still never spawns (P3b review, finding #1 observability)."""
    st = _state(council_cost_usd=0.0,
                council_spec={"mode": "fusion", "synth_backend": "minimax",
                              "seats": ["framer"],
                              "bounds": {"grounding": "on", "max_usd": 0.1}})

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("must not spawn when the ceiling can't fund a spawn")

    with patch("core.backends.claude_code.ClaudeCodeClient", _Boom):
        out = await grounding_node(st)
    assert "ceiling" in out["error"].lower()
    assert "could not run" in out["error"].lower()    # the misconfig hint
    assert "0.10" in out["error"]
    assert "council_restart" not in out


def test_compute_drift_threshold_arg_overrides_default():
    """compute_drift takes a per-run threshold; None falls back to the global."""
    # 2 of 3 contradicted = 0.66.
    claims = [
        {"claim": "a", "status": "contradicted", "note": ""},
        {"claim": "b", "status": "contradicted", "note": ""},
        {"claim": "c", "status": "confirmed", "note": ""},
    ]
    assert compute_drift(claims, 0.1)["should_restart"] is True    # 0.66 ≥ 0.1
    assert compute_drift(claims, 0.9)["should_restart"] is False   # 0.66 < 0.9
    assert compute_drift(claims, None)["should_restart"] is True   # default 0.34


async def test_grounding_bounds_off_is_runtime_noop():
    """bounds.grounding='off' → grounding_node is a pure no-op converge: it never
    spawns the verifier and commits nothing extra (synthesize already committed
    in-node), regardless of the global cadence being ON."""
    st = _state(council_spec={"mode": "fusion", "synth_backend": "minimax",
                              "seats": ["framer"], "bounds": {"grounding": "off"}})
    spawned = {"called": False}

    class _Boom:
        def __init__(self, *a, **k):
            spawned["called"] = True

        async def chat(self, *a, **k):  # pragma: no cover
            raise AssertionError("a grounding-off run must not spawn")

    with patch("core.backends.claude_code.ClaudeCodeClient", _Boom):
        out = await grounding_node(st)
    assert spawned["called"] is False
    assert "council_restart" not in out                       # converge
    assert "messages" not in out                              # no pending → no commit
    assert out["grounding_verdict"] is None
    assert (out.get("council_spec") or {}).get("reseed_context") is None


async def test_grounding_bounds_lower_max_usd_trips_ceiling_earlier():
    """A per-profile max_usd below the global $5 trips the cost ceiling earlier:
    prior cost over the per-run ceiling → fail-loud, no spawn, per-run ceiling in
    the notice (not the global 5.00)."""
    st = _state(council_cost_usd=0.5,
                council_spec={"mode": "fusion", "synth_backend": "minimax",
                              "seats": ["framer"],
                              "bounds": {"grounding": "on", "max_usd": 0.4}})

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("must not spawn after a per-run ceiling breach")

    with patch("core.backends.claude_code.ClaudeCodeClient", _Boom):
        out = await grounding_node(st)
    assert out["error"] and "ceiling" in out["error"].lower()
    assert "0.40" in out["error"]              # the per-run ceiling, not global 5.00
    assert "5.00" not in out["error"]
    assert "council_restart" not in out


async def test_grounding_bounds_restart_budget_zero_suppresses_restart():
    """A per-profile restart_budget of 0 converges on drift instead of restarting,
    even though the default budget (2) would restart; residual drift is flagged."""
    claims = [{"claim": "a", "status": "contradicted", "note": ""},
              {"claim": "b", "status": "contradicted", "note": ""}]
    st = _state(council_restart=0,
                council_spec={"mode": "fusion", "synth_backend": "minimax",
                              "seats": ["framer"],
                              "bounds": {"grounding": "on", "restart_budget": 0}})
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims))):
        out = await grounding_node(st)
    assert "council_restart" not in out                       # converge, budget 0
    assert (out.get("council_spec") or {}).get("reseed_context") is None
    flagged = out["council_handoff"]["active_debate"]
    assert any("CONTRADICTED" in f for f in flagged)


async def test_grounding_bounds_high_drift_threshold_suppresses_restart():
    """A per-profile drift_threshold above the contradicted ratio converges where
    the default (0.34) would restart."""
    # 2 of 3 contradicted = 0.66 ≥ 0.34 (default → restart) but < 0.9 (per-run).
    claims = [
        {"claim": "a", "status": "contradicted", "note": ""},
        {"claim": "b", "status": "contradicted", "note": ""},
        {"claim": "c", "status": "confirmed", "note": ""},
    ]
    st = _state(council_restart=0,
                council_spec={"mode": "fusion", "synth_backend": "minimax",
                              "seats": ["framer"],
                              "bounds": {"grounding": "on", "drift_threshold": 0.9}})
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims))):
        out = await grounding_node(st)
    assert "council_restart" not in out                       # converge (0.66 < 0.9)
    assert (out.get("council_spec") or {}).get("reseed_context") is None


async def test_grounding_bounds_unset_falls_back_to_globals():
    """A council run whose spec carries an EMPTY bounds dict behaves byte-identically
    to pre-P3b: global drift threshold drives the restart (no per-run override)."""
    # 2 of 3 contradicted = 0.66 ≥ 0.34 global default → restart.
    claims = [
        {"claim": "a", "status": "contradicted", "note": ""},
        {"claim": "b", "status": "contradicted", "note": ""},
        {"claim": "c", "status": "confirmed", "note": ""},
    ]
    st = _state(council_restart=0,
                council_spec={"mode": "fusion", "synth_backend": "minimax",
                              "seats": ["framer"], "bounds": {}})
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(claims, research="corrected"))):
        out = await grounding_node(st)
    assert out["council_restart"] == 1                        # global threshold applied
    assert out["council_spec"]["reseed_context"]
