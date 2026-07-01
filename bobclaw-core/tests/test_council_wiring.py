"""
BoBClaw Core — CoCouncil P1b wiring tests (network-free).

Covers (per the P1b brief + design §Test plan):
  * fusion / panel_dispatch: exactly len(seats) Sends, identical task across
    seats, DISTINCT backends resolved per posture, deterministic idx merge.
  * synthesize_node: reads all panel_results (sorted), emits one answer, parses
    the COUNCIL HANDOFF block.
  * sequential: council_node runs the 3-voice chain (mock backends), emits
    answer + handoff.
  * routing isolation: a non-council face stays on the dispatch path; council-max
    + each mode routes to the right council node.
  * face registry: 17 faces; council-max + council-lite present, old council gone.

All backend calls are mocked by patching ``_send_to_backend`` (the critic seam),
so nothing hits the network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.types import Send

from core.config import COUNCIL_DEFAULT_SEATS, COUNCIL_SEAT_BACKENDS
from core.faces.registry import FaceRegistry
from core.graph import _route_after_recall
from core.nodes.panel import (
    _COUNCIL_SYSTEM_BASE,
    _route_after_panel,
    panel_dispatch_node,
    panel_worker_node,
    resolve_seat_backend,
)
from core.nodes.route import route_node

# A valid COUNCIL HANDOFF block the engine's parser round-trips.
_HANDOFF_OUTPUT = """\
The council reconciles the panel as follows: option A is the strongest.

### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** IDEA-01, IDEA-02
- **[ACTIVE DEBATE]:** IDEA-03
- **[BLOCKED]:** budget sign-off
- **[CORRECTION]:** None
- **[NEXT TASK]:** @Human: approve the budget
"""


# ─── seat selector (design table E) ──────────────────────────────────────────

def test_resolve_seat_backend_table_e_defaults():
    framer, framer_fb, _ = resolve_seat_backend("framer")
    stress, _, _ = resolve_seat_backend("stress")
    wildcard, _, _ = resolve_seat_backend("wildcard")
    synth, _, _ = resolve_seat_backend("synth")
    assert framer == "claude_api"
    assert stress == "gemini_flash"
    assert wildcard == "deepseek_v4_flash"
    assert synth == "minimax"
    # framer carries a fallback chain (providers can revoke access).
    assert framer_fb and framer_fb[0] == "gemini_pro"


def test_resolve_seat_backend_unknown_posture_degrades():
    """An unknown posture falls back to the framer entry, not a crash."""
    backend, _, _ = resolve_seat_backend("nonsense-posture")
    assert backend == COUNCIL_SEAT_BACKENDS["framer"]["backend"]


def test_resolve_seat_backend_profile_override_wins():
    profile = {"framer": {"backend": "local", "fallback_chain": ["claude_api"]}}
    backend, fb, _ = resolve_seat_backend("framer", profile)
    assert backend == "local"
    assert fb == ["claude_api"]


def test_resolve_seat_backend_carries_role_prompt():
    profile = {"framer": {"backend": "local", "role_prompt": "be the optimist"}}
    backend, _, rp = resolve_seat_backend("framer", profile)
    assert backend == "local" and rp == "be the optimist"
    _, _, default_rp = resolve_seat_backend("stress")
    assert default_rp == ""  # built-in postures carry no role prompt


async def test_panel_worker_injects_role_prompt_into_system():
    seen = {}

    async def _capture(messages, backend):
        seen["system"] = messages[0]["content"]
        return "voice"

    sub = {"seat_posture": "framer", "backend": "claude_api", "fallback_chain": [],
           "task": "shared", "seat_idx": 0, "role_prompt": "argue the risk case", "messages": []}
    with patch("core.nodes.panel._send_to_backend", _capture):
        await panel_worker_node(sub)
    assert "argue the risk case" in seen["system"]
    assert _COUNCIL_SYSTEM_BASE in seen["system"]  # base constitution still present


async def test_panel_worker_no_role_prompt_is_base_only():
    seen = {}

    async def _capture(messages, backend):
        seen["system"] = messages[0]["content"]
        return "voice"

    sub = {"seat_posture": "framer", "backend": "claude_api", "fallback_chain": [],
           "task": "shared", "seat_idx": 0, "messages": []}
    with patch("core.nodes.panel._send_to_backend", _capture):
        await panel_worker_node(sub)
    assert seen["system"] == _COUNCIL_SYSTEM_BASE  # byte-identical when no role prompt


# ─── fusion: panel_dispatch + replication Send ───────────────────────────────

def _fusion_state(**overrides) -> dict:
    base = {
        "task": "Should we adopt the parallel panel pattern?",
        "face_id": "council-max",
        "council_spec": {"mode": "fusion", "seats": list(COUNCIL_DEFAULT_SEATS),
                         "synth_backend": "minimax"},
        "messages": [],
        "panel_results": [],
    }
    base.update(overrides)
    return base


def test_panel_dispatch_resolves_all_seats():
    st = _fusion_state()
    delta = panel_dispatch_node(st)
    resolved = delta["council_spec"]["resolved_seats"]
    assert len(resolved) == len(COUNCIL_DEFAULT_SEATS)
    assert [s["idx"] for s in resolved] == list(range(len(COUNCIL_DEFAULT_SEATS)))
    assert delta["council_spec"]["panel_task"]


def test_panel_route_emits_exactly_len_seats_sends():
    st = _fusion_state()
    st.update(panel_dispatch_node(st))
    sends = _route_after_panel(st)
    assert isinstance(sends, list)
    assert len(sends) == len(COUNCIL_DEFAULT_SEATS)
    for s in sends:
        assert isinstance(s, Send)
        assert s.node == "panel_worker"


def test_panel_sends_carry_identical_task():
    """Fusion = every seat answers the SAME prompt blind."""
    st = _fusion_state()
    st.update(panel_dispatch_node(st))
    sends = _route_after_panel(st)
    tasks = {s.arg["task"] for s in sends}
    assert len(tasks) == 1  # all identical


def test_panel_sends_resolve_distinct_backends():
    """The default panel seats resolve to distinct backends (table E)."""
    st = _fusion_state()
    st.update(panel_dispatch_node(st))
    sends = _route_after_panel(st)
    backends = [s.arg["backend"] for s in sends]
    assert len(set(backends)) == len(backends)  # all distinct
    # Each Send carries its seat index for deterministic merge.
    assert sorted(s.arg["seat_idx"] for s in sends) == list(range(len(sends)))


def test_panel_route_empty_spec_falls_through_to_synthesize():
    st = _fusion_state(council_spec={"mode": "fusion", "seats": [],
                                     "synth_backend": "minimax"})
    st.update(panel_dispatch_node(st))
    route = _route_after_panel(st)
    assert route == "synthesize"


# ─── fusion: panel_worker (backend + fallback walk) ──────────────────────────

async def test_panel_worker_returns_idx_carrying_entry():
    sub = {
        "seat_posture": "framer",
        "backend": "claude_api",
        "fallback_chain": ["gemini_pro"],
        "task": "the shared prompt",
        "seat_idx": 2,
        "messages": [],
    }
    with patch("core.nodes.panel._send_to_backend",
               AsyncMock(return_value="framer voice text")):
        out = await panel_worker_node(sub)
    assert out["panel_results"] == [
        {"idx": 2, "posture": "framer", "backend": "claude_api",
         "text": "framer voice text", "round": 0}
    ]


async def test_panel_worker_walks_fallback_on_error():
    """Primary backend fails → the fallback chain is walked; entry records it."""
    calls = []

    async def _flaky(messages, backend):
        calls.append(backend)
        if backend == "claude_api":
            raise RuntimeError("primary down")
        return "fallback voice text"

    sub = {
        "seat_posture": "framer",
        "backend": "claude_api",
        "fallback_chain": ["gemini_pro"],
        "task": "shared",
        "seat_idx": 0,
        "messages": [],
    }
    with patch("core.nodes.panel._send_to_backend", _flaky):
        out = await panel_worker_node(sub)
    entry = out["panel_results"][0]
    assert entry["text"] == "fallback voice text"
    assert entry["backend"] == "gemini_pro"
    assert calls == ["claude_api", "gemini_pro"]


def test_panel_results_merge_deterministically_by_idx():
    """The operator.add reducer accumulates entries in arbitrary order; the
    consumer sorts by idx. Simulate out-of-order arrival."""
    merged = []
    for entry in (
        {"idx": 2, "posture": "wildcard", "backend": "deepseek_v4_flash", "text": "c"},
        {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "a"},
        {"idx": 1, "posture": "stress", "backend": "gemini_flash", "text": "b"},
    ):
        merged = merged + [entry]  # operator.add semantics
    ordered = [e["text"] for e in sorted(merged, key=lambda r: r["idx"])]
    assert ordered == ["a", "b", "c"]


# ─── fusion: synthesize ──────────────────────────────────────────────────────

async def test_synthesize_reads_results_emits_answer_and_handoff(monkeypatch):
    from core.nodes.synthesize import synthesize_node

    # P2: synthesize defers the commit to grounding_node when grounding is ON.
    # Force grounding OFF so this test keeps asserting the in-node commit
    # (_grounding_on re-reads core.config.COUNCIL_GROUND_CADENCE at call time).
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)

    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "panel_results": [
            {"idx": 1, "posture": "stress", "backend": "gemini_flash", "text": "B"},
            {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"},
        ],
        "messages": [],
    }
    seen = {}

    async def _synth(messages, backend):
        seen["backend"] = backend
        seen["prompt"] = messages[-1]["content"]
        return _HANDOFF_OUTPUT

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await synthesize_node(state)

    assert seen["backend"] == "minimax"
    # The synth prompt sees BOTH seats (sorted: framer A then stress B).
    assert "A" in seen["prompt"] and "B" in seen["prompt"]
    # One assistant message carrying the synthesized answer.
    assert out["messages"][0]["role"] == "assistant"
    assert "strongest" in out["messages"][0]["content"]
    # Grounding OFF → committed in-node, nothing deferred.
    assert out["council_pending_answer"] is None
    # Handoff parsed.
    handoff = out["council_handoff"]
    assert handoff["resolved"] == ["IDEA-01", "IDEA-02"]
    assert handoff["active_debate"] == ["IDEA-03"]
    assert handoff["blocked"] == ["budget sign-off"]
    assert "approve the budget" in handoff["next_task"]


async def test_synthesize_fails_soft_on_backend_error():
    from core.nodes.synthesize import synthesize_node

    state = {
        "task": "t",
        "council_spec": {"synth_backend": "minimax"},
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"}],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend",
               AsyncMock(side_effect=RuntimeError("synth down"))), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await synthesize_node(state)
    assert out["error"]
    assert out["council_handoff"] is None
    assert out["messages"][0]["role"] == "assistant"


async def test_synthesize_flags_degraded_seat(monkeypatch):
    """A seat whose whole backend chain failed comes back with empty text; the
    answer must carry a visible 'ran with N of M voices' note and the synth prompt
    must show the seat as unavailable (not a silent blank voice)."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)

    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"},
            {"idx": 1, "posture": "stress", "backend": "gemini_flash",
             "text": "", "error": "all backends down"},
        ],
        "messages": [],
    }
    seen = {}

    async def _synth(messages, backend):
        seen["prompt"] = messages[-1]["content"]
        return _HANDOFF_OUTPUT

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await synthesize_node(state)

    # Synthesizer is TOLD the seat was unavailable rather than handed a blank voice.
    assert "seat unavailable" in seen["prompt"]
    assert "A" in seen["prompt"]  # the healthy seat still contributes
    # Committed answer carries a visible degradation note naming the lost seat.
    answer = out["messages"][0]["content"]
    assert "Council ran with 1 of 2 voices" in answer
    assert "unavailable: stress" in answer


# ─── sequential: council_node ────────────────────────────────────────────────

async def test_council_node_runs_three_voice_chain():
    from core.nodes import council as council_mod

    state = {
        "task": "decide the architecture",
        "council_spec": {"mode": "sequential", "synth_backend": "minimax"},
        "messages": [],
    }

    # The engine drives 3 sequential backend calls through make_backend_fn →
    # core.nodes.panel._send_to_backend. The synth (3rd) call returns the handoff.
    call_order = []

    async def _seq(messages, backend):
        call_order.append(backend)
        if len(call_order) == 3:
            return _HANDOFF_OUTPUT
        return f"voice {len(call_order)} on {backend}"

    with patch("core.nodes.panel._send_to_backend", _seq), \
         patch("core.nodes.council._append_agent_turn_event", AsyncMock()), \
         patch.object(council_mod.CouncilEngine, "save_session_log", lambda self, s: ""):
        out = await council_node_run(council_mod, state)

    assert len(call_order) == 3  # framer → stress → synth
    assert out["messages"][0]["role"] == "assistant"
    assert "strongest" in out["messages"][0]["content"]
    assert out["council_handoff"]["resolved"] == ["IDEA-01", "IDEA-02"]


async def council_node_run(council_mod, state):
    return await council_mod.council_node(state)


# ─── routing isolation ───────────────────────────────────────────────────────

def test_route_after_recall_non_council_goes_to_dispatch():
    """A non-council face (no council_spec) keeps the existing dispatch path."""
    assert _route_after_recall({"face_id": "assistant"}) == "dispatch"
    assert _route_after_recall({"face_id": "worker-kimi", "council_spec": None}) == "dispatch"


def test_route_after_recall_fusion_and_sequential():
    assert _route_after_recall({"council_spec": {"mode": "fusion"}}) == "panel_dispatch"
    assert _route_after_recall({"council_spec": {"mode": "sequential"}}) == "council"
    # default mode (unspecified) → fusion
    assert _route_after_recall({"council_spec": {}}) == "panel_dispatch"


async def test_route_node_sets_council_spec_only_for_council_max():
    # council-max → council_spec set.
    out = await route_node({"face_id": "council-max", "task": "x"})
    assert out["council_spec"]["mode"] == "fusion"
    assert out["council_spec"]["seats"] == list(COUNCIL_DEFAULT_SEATS)
    assert out["council_spec"]["synth_backend"] == "minimax"


async def test_route_node_council_max_model_override_picks_mode():
    out = await route_node({"face_id": "council-max", "task": "x",
                            "model_override": "sequential"})
    assert out["council_spec"]["mode"] == "sequential"


async def test_route_node_non_council_face_has_no_council_spec(monkeypatch):
    """A normal face's route result never carries council_spec (isolation)."""
    from core.nodes import route as route_mod

    async def _no_local():
        return []

    monkeypatch.setattr(route_mod._router, "discover", AsyncMock(return_value=[]))
    out = await route_node({"face_id": "assistant", "task": "hello"})
    assert "council_spec" not in out


# ─── profile-driven council (P3a) ─────────────────────────────────────────────

def test_build_council_spec_from_profile():
    from core.nodes.route import _build_council_spec_from_profile
    prof = {
        "shape": "fusion",
        "seats": [
            {"posture": "framer", "role_prompt": "optimist"},
            {"posture": "stress", "backend": "gemini_flash",
             "fallback_chain": ["minimax"], "role_prompt": "skeptic"},
        ],
        "synth_backend": "minimax",
        "protocol_bounds": {"max_usd": 1.0},
    }
    spec = _build_council_spec_from_profile(prof)
    assert spec["mode"] == "fusion"
    assert spec["seats"] == ["framer", "stress"]
    assert spec["synth_backend"] == "minimax"
    assert spec["profile"]["framer"] == {"role_prompt": "optimist"}  # no backend → inherits table
    assert spec["profile"]["stress"]["backend"] == "gemini_flash"
    assert spec["bounds"]["max_usd"] == 1.0


async def test_route_node_profile_drives_council(tmp_path):
    from core import teams
    teams.set_custom_teams_dir(tmp_path)
    try:
        teams.create_profile("council-fast", {
            "seats": [
                {"posture": "framer", "role_prompt": "be the optimist"},
                {"posture": "stress", "backend": "gemini_flash"},
            ],
            "shape": "fusion",
            "synth_backend": "minimax",
            "protocol_bounds": {"grounding": "off"},
        })
        out = await route_node({"face_id": "assistant", "task": "x", "profile_name": "council-fast"})
        spec = out["council_spec"]
        assert spec["mode"] == "fusion"
        assert spec["seats"] == ["framer", "stress"]
        assert spec["profile"]["framer"]["role_prompt"] == "be the optimist"
        assert spec["bounds"]["grounding"] == "off"
    finally:
        teams.set_custom_teams_dir(None)


def test_build_council_spec_derives_seats_from_roles():
    """A council-shaped profile with only a roster (no explicit seats) derives its
    council seats from each role's primary slot — one builder authors JOAT + council."""
    from core.nodes.route import _build_council_spec_from_profile
    prof = {
        "shape": "fusion",
        "roles": {
            "apex": [{"name": "", "backend": "claude_api", "escalation_chain": [], "role_prompt": "lead"}],
            "worker": [{"name": "", "backend": "deepseek_v4_flash",
                        "escalation_chain": ["glm_5_2"], "role_prompt": "do"}],
        },
    }
    spec = _build_council_spec_from_profile(prof)
    assert set(spec["seats"]) == {"apex", "worker"}
    assert spec["profile"]["apex"]["backend"] == "claude_api"
    assert spec["profile"]["apex"]["role_prompt"] == "lead"
    assert spec["profile"]["worker"]["fallback_chain"] == ["glm_5_2"]


# ─── face registry ───────────────────────────────────────────────────────────

def test_registry_has_24_faces_with_council_split():
    reg = FaceRegistry()
    assert len(reg) == 24
    ids = {f.id for f in reg.list_faces()}
    assert "council-max" in ids
    assert "council-lite" in ids
    assert "council" not in ids  # old mock id gone
    # council-max holds no tools (off the long-agentic path).
    assert reg.get_allowed_tools("council-max") == []
