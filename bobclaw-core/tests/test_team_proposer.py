"""BoBClaw Core — tests for the team-builder proposer (network-free via injected send)."""
from __future__ import annotations

import pytest

from core import team_proposer


@pytest.mark.asyncio
async def test_propose_parses_roles_and_slugifies_name():
    async def fake_send(messages, backend):
        assert backend == "deepseek_v4_flash"
        # the prompt carries the palette + goal
        assert "deepseek_v4_flash" in messages[-1]["content"]
        return (
            '{"name": "Cheap Fleet", "roles": {'
            '"apex": {"backend": "minimax", "escalation_chain": ["claude_api"]}, '
            '"worker": {"backend": "deepseek_v4_flash"}, '
            '"critic": {"backend": "local"}}}'
        )

    out = await team_proposer.propose_team("keep it cheap", send=fake_send)
    assert out["name"] == "cheap fleet"  # slugified (lowercased)
    assert out["roles"]["apex"]["backend"] == "minimax"
    assert out["roles"]["apex"]["escalation_chain"] == ["claude_api"]
    assert out["roles"]["worker"]["escalation_chain"] == []  # normalized
    assert set(out["roles"]) == {"apex", "worker", "critic"}


@pytest.mark.asyncio
async def test_propose_extracts_json_from_prose_and_drops_unknown_backend():
    async def fake_send(messages, backend):
        return (
            "Sure! Here's a fleet:\n"
            '{"roles": {"worker": {"backend": "totally-fake"}, '
            '"critic": {"backend": "local"}}}\nHope that helps."'
        )

    out = await team_proposer.propose_team("x", send=fake_send)
    assert "worker" not in out["roles"]  # unknown backend dropped
    assert out["roles"]["critic"]["backend"] == "local"
    assert out["name"] == ""


@pytest.mark.asyncio
async def test_propose_handles_backend_failure():
    async def boom(messages, backend):
        raise RuntimeError("backend down")

    out = await team_proposer.propose_team("x", send=boom)
    assert out["roles"] == {}
    assert out["error"] == "backend down"


@pytest.mark.asyncio
async def test_propose_handles_no_json_reply():
    async def fake_send(messages, backend):
        return "I cannot help with that."

    out = await team_proposer.propose_team("x", send=fake_send)
    assert out["roles"] == {}
    assert out["raw"] == "I cannot help with that."


# ── multi-turn refine ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refine_parses_multislot_draft_and_prose():
    async def fake_send(messages, backend):
        return (
            "Added a second worker on glm.\n"
            '{"name":"my-fleet","roles":{"worker":['
            '{"name":"bulk","backend":"deepseek_v4_flash","escalation_chain":["glm_5_2"]},'
            '{"name":"tool","backend":"glm_5_2","escalation_chain":[]}],'
            '"critic":[{"backend":"local"}]}}'
        )

    out = await team_proposer.refine_team("add a glm worker", send=fake_send)
    assert out["reply"].startswith("Added a second worker")
    assert [s["backend"] for s in out["draft"]["roles"]["worker"]] == ["deepseek_v4_flash", "glm_5_2"]
    assert out["draft"]["roles"]["critic"][0]["backend"] == "local"
    assert out["draft"]["name"] == "my-fleet"


@pytest.mark.asyncio
async def test_refine_drops_unknown_backend_slots():
    async def fake_send(messages, backend):
        return '{"roles":{"worker":[{"backend":"totally-fake"},{"backend":"local"}]}}'

    out = await team_proposer.refine_team("x", send=fake_send)
    assert [s["backend"] for s in out["draft"]["roles"]["worker"]] == ["local"]


@pytest.mark.asyncio
async def test_refine_keeps_prior_draft_on_backend_error():
    prior = {"name": "keep", "roles": {"worker": [{"name": "", "backend": "local", "escalation_chain": []}]}}

    async def boom(messages, backend):
        raise RuntimeError("down")

    out = await team_proposer.refine_team("x", draft=prior, send=boom)
    assert out["draft"] == prior
    assert out["error"] == "down"


@pytest.mark.asyncio
async def test_refine_threads_history():
    captured = {}

    async def fake_send(messages, backend):
        captured["msgs"] = messages
        return 'ok\n{"roles":{"worker":[{"backend":"local"}]}}'

    hist = [{"role": "user", "content": "make a cheap fleet"},
            {"role": "assistant", "content": "done"}]
    await team_proposer.refine_team("now add a critic", history=hist, send=fake_send)
    assert [m["role"] for m in captured["msgs"]] == ["system", "user", "assistant", "user"]
    assert "now add a critic" in captured["msgs"][-1]["content"]


@pytest.mark.asyncio
async def test_refine_keeps_role_prompt_shape_and_bounds():
    async def fake_send(messages, backend):
        return (
            "Set it up.\n"
            '{"name":"p","roles":{"worker":[{"backend":"deepseek_v4_flash",'
            '"role_prompt":"be terse"}]},"shape":"fusion",'
            '"protocol_bounds":{"max_usd":2.0,"grounding":"off"}}'
        )

    out = await team_proposer.refine_team("make a fusion profile", send=fake_send)
    d = out["draft"]
    assert d["roles"]["worker"][0]["role_prompt"] == "be terse"
    assert d["shape"] == "fusion"
    assert d["protocol_bounds"]["max_usd"] == 2.0
    assert d["protocol_bounds"]["grounding"] == "off"


@pytest.mark.asyncio
async def test_refine_drops_unknown_shape():
    async def fake_send(messages, backend):
        return '{"roles":{"worker":[{"backend":"local"}]},"shape":"spiral"}'

    out = await team_proposer.refine_team("x", send=fake_send)
    assert "shape" not in out["draft"]
