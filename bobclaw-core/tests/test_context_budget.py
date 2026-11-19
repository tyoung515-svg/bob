"""Context budget: per-backend windows, history fitting, transport guard, turn-event stamping."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import config
from core.context import (
    build_prior_conversation,
    enforce_backend_limits,
    fit_history,
    history_budget_tokens,
    limits_for,
    prompt_char_cap,
    render_prior_conversation,
    trim_prior_conversation_text,
)
from core.nodes._l0_events import _append_agent_turn_event


def _hist(n: int, size: int = 100, start_id: int = 1) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i} " + "x" * size,
            "id": str(start_id + i),
            "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00",
        }
        for i in range(n)
    ]


# ── fit_history ──────────────────────────────────────────────────────────────

def test_under_budget_keeps_everything_without_a_marker():
    fit = fit_history(_hist(6), budget_tokens=100_000)
    assert len(fit.kept) == 6 and fit.omitted == [] and fit.marker is None and fit.head_count == 0
    assert render_prior_conversation(fit).startswith("Prior conversation:\nuser: m0 ")


def test_over_budget_keeps_the_opening_exchange_and_the_newest_tail_and_says_so():
    fit = fit_history(_hist(50, size=100), budget_tokens=1_000)
    assert fit.tokens_after <= 1_000 and fit.head_count == 2
    head, tail = fit.kept[:2], fit.kept[2:]
    assert [m["id"] for m in head] == ["1", "2"]                       # the goal / constraints survive
    ids = [int(m["id"]) for m in tail]
    assert ids[-1] == 50 and ids == list(range(ids[0], 51))            # contiguous newest tail, no gaps
    assert len(fit.kept) + len(fit.omitted) == 50
    assert fit.omitted[0]["id"] == "3" and int(fit.omitted[-1]["id"]) == ids[0] - 1
    assert fit.marker and f"{len(fit.omitted)} message(s)" in fit.marker
    assert "message ids 3.." in fit.marker and "between the opening exchange" in fit.marker
    lines = render_prior_conversation(fit).split("\n")
    assert lines[1].startswith("user: m0 ") and lines[2].startswith("assistant: m1 ")
    assert lines[3].startswith("[context note:")                       # the gap is announced where it is


def test_short_histories_get_no_head_protection():
    fit = fit_history(_hist(3, size=2_000), budget_tokens=400)
    assert fit.head_count == 0 and fit.kept[-1]["id"] == "3"


def test_newest_message_over_budget_is_truncated_not_dropped_with_everything_else():
    history = [
        {"role": "user", "content": "old question " + "q" * 600, "id": "1"},
        {"role": "assistant", "content": "A" * 50_000, "id": "2"},
    ]
    fit = fit_history(history, budget_tokens=500)
    assert fit.tokens_after <= 500
    assert len(fit.kept) == 1 and fit.kept[0]["id"] == "2"
    assert "characters omitted from this message" in fit.kept[0]["content"]
    assert fit.kept[0]["content"].startswith("A") and fit.kept[0]["content"].endswith("A")
    assert fit.truncated_ids == ["2"] and len(fit.omitted) == 1 and fit.marker


def test_huge_newest_message_keeps_the_opening_and_a_truncated_newest():
    history = _hist(6, size=100) + [{"role": "assistant", "content": "Z" * 60_000, "id": "7"}]
    fit = fit_history(history, budget_tokens=1_000)
    assert [m["id"] for m in fit.kept[:2]] == ["1", "2"]
    assert fit.kept[-1]["id"] == "7" and "characters omitted" in fit.kept[-1]["content"]
    assert fit.tokens_after <= 1_000


def test_a_huge_opening_message_is_shrunk_not_dropped():
    history = [{"role": "user", "content": "GOAL " + "g" * 80_000, "id": "1"}] + _hist(30, size=100, start_id=2)
    fit = fit_history(history, budget_tokens=2_000)
    assert fit.kept[0]["id"] == "1" and fit.kept[0]["content"].startswith("GOAL")
    assert "characters omitted" in fit.kept[0]["content"] and "1" in fit.truncated_ids
    assert fit.tokens_after <= 2_000


def test_empty_messages_are_skipped():
    fit = fit_history([{"role": "user", "content": ""}, {"role": "user", "content": None},
                       {"role": "assistant", "content": "real"}], budget_tokens=1_000)
    assert [m["content"] for m in fit.kept] == ["real"]


# ── limits / budgets ─────────────────────────────────────────────────────────

def test_cloud_models_get_half_their_real_window():
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 500_000
    assert history_budget_tokens(limits_for("codex_code")) == 525_000
    assert history_budget_tokens(limits_for("kimi_platform")) == 131_072


def test_strata_uses_its_128k_window():
    assert limits_for("qwen_research").window_tokens == 131_072
    assert history_budget_tokens(limits_for("qwen_research")) == 98_304   # 75% of 131072
    assert history_budget_tokens(limits_for("local_strata")) == 98_304
    assert prompt_char_cap(limits_for("qwen_research")) == 372_000        # 124K-token hard cap


def test_unverified_local_models_stay_conservative():
    assert history_budget_tokens(limits_for("local")) == 6_400            # 80% of an 8K-token cap


def test_argv_backends_are_capped_by_transport():
    assert prompt_char_cap(limits_for("kimi_cli")) == 100_000
    assert prompt_char_cap(limits_for("agy_code")) == 100_000
    assert prompt_char_cap(limits_for("pi_code")) == 100_000
    assert history_budget_tokens(limits_for("kimi_cli")) == 26_666
    assert prompt_char_cap(limits_for("deepseek_v4_flash")) is None


def test_unknown_backend_is_conservative():
    assert history_budget_tokens(limits_for(None)) == 64_000
    assert history_budget_tokens(limits_for("brand_new_backend")) == 64_000


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BOB_CTX_HISTORY_FRACTION", "0.25")
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 250_000
    assert history_budget_tokens(limits_for("qwen_research")) == 32_768   # global env beats Strata's 0.75
    monkeypatch.setenv("BOB_CTX_HISTORY_FRACTION", "0.99")
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 900_000   # clamped to 0.9
    monkeypatch.setenv("BOB_CTX_HISTORY_FRACTION", "junk")
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 500_000
    monkeypatch.setenv("BOB_CTX_MAX_PROMPT_TOKENS_QWEN_RESEARCH", "40000")
    assert limits_for("qwen_research").max_prompt_tokens == 40_000
    monkeypatch.setenv("BOB_CTX_WINDOW_GLM_5_3", "200000")
    assert limits_for("glm_5_3").window_tokens == 200_000


def test_long_profile_is_the_700k_option(monkeypatch):
    monkeypatch.setenv("BOB_CTX_PROFILE", "long")
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 750_000
    assert history_budget_tokens(limits_for("codex_code")) == 787_500
    assert history_budget_tokens(limits_for("kimi_cli")) == 26_666          # the transport cap still wins
    monkeypatch.setenv("BOB_CTX_HISTORY_FRACTION", "0.6")
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 600_000  # an explicit fraction beats the profile
    monkeypatch.delenv("BOB_CTX_HISTORY_FRACTION")
    monkeypatch.setenv("BOB_CTX_PROFILE", "nonsense")
    assert history_budget_tokens(limits_for("deepseek_v4_flash")) == 500_000  # unknown profile is ignored


# ── build_prior_conversation ─────────────────────────────────────────────────

def test_build_keeps_the_legacy_line_format():
    text, meta = build_prior_conversation(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        preferred_backend="deepseek_v4_flash",
    )
    assert text == "Prior conversation:\nuser: hi\nassistant: hello"
    assert meta["messages_in"] == 2 and meta["messages_kept"] == 2 and meta["messages_omitted"] == 0
    assert meta["budget_tokens"] == 500_000 and meta["window_tokens"] == 1_000_000


def test_override_beats_preferred_backend():
    _text, meta = build_prior_conversation(_hist(3), preferred_backend="deepseek_v4_flash",
                                           backend_override="local")
    assert meta["backend"] == "local" and meta["budget_tokens"] == 6_400


def test_kill_switch_restores_the_legacy_flatten(monkeypatch):
    monkeypatch.setenv("BOB_CONTEXT_FIT_ENABLED", "false")
    history = _hist(4, size=50)
    text, meta = build_prior_conversation(history, preferred_backend="local")
    assert meta is None
    assert text == "Prior conversation:\n" + "\n".join(f"{m['role']}: {m['content']}" for m in history)
    messages = [{"role": "system", "content": "Prior conversation:\n" + "x" * 500_000}]
    assert enforce_backend_limits(messages, "kimi_cli") is messages


# ── enforce_backend_limits ───────────────────────────────────────────────────

def _prior(n: int, size: int) -> str:
    return "Prior conversation:\n" + "\n".join(f"{m['role']}: {m['content']}" for m in _hist(n, size=size))


def test_big_window_http_backend_is_left_alone():
    messages = [{"role": "system", "content": _prior(500, 4_000)}, {"role": "user", "content": "q"}]
    assert enforce_backend_limits(messages, "deepseek_v4_flash") is messages


def test_argv_backend_gets_the_middle_trimmed_and_keeps_the_opening():
    prior = _prior(300, 1_000)
    messages = [{"role": "system", "content": prior}, {"role": "user", "content": "the new question"}]
    out = enforce_backend_limits(messages, "kimi_cli")
    assert sum(len(m["content"]) for m in out) <= 100_000
    assert out[1] == messages[1]
    body = out[0]["content"]
    assert body.startswith("Prior conversation:\nuser: m0 ")               # opening exchange survives
    assert "assistant: m1 " in body[:3_000]
    note_at = body.index("[context note: part of this conversation")
    after_note = body[note_at:].split("]\n", 1)[1]
    assert after_note.startswith(("user: ", "assistant: "))                # cut on a turn boundary
    assert prior.rstrip().endswith(after_note.rstrip()[-80:])               # newest content survives
    assert messages[0]["content"] == prior                                  # input not mutated


def test_strata_cap_applies_only_beyond_its_128k_window_share():
    small = [{"role": "system", "content": _prior(100, 2_000)}]              # ~200K chars: fits 372K
    assert enforce_backend_limits(small, "qwen_research") is small
    big = [{"role": "system", "content": _prior(400, 1_000)}]                # ~400K chars: over the cap
    out = enforce_backend_limits(big, "qwen_research")
    assert sum(len(m["content"]) for m in out) <= 372_000


def test_no_prior_block_means_nothing_to_trim():
    messages = [{"role": "user", "content": "y" * 300_000}]
    assert enforce_backend_limits(messages, "kimi_cli") is messages


def test_trim_text_is_a_noop_when_it_already_fits():
    text = "Prior conversation:\nuser: a\nassistant: b"
    assert trim_prior_conversation_text(text, 10_000) == text


# ── execute hooks ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_and_collect_applies_the_backend_cap():
    from core.nodes import execute

    seen: dict = {}

    async def fake_stream(messages, backend, model_override):
        seen["total"] = sum(len(m["content"]) for m in messages)
        yield "ok"

    messages = [{"role": "system", "content": _prior(300, 1_000)}, {"role": "user", "content": "q"}]
    with patch.object(execute, "_stream_to_backend", fake_stream):
        assert await execute._stream_and_collect(messages, "kimi_cli", None, None) == "ok"
        assert seen["total"] <= 100_000
        await execute._stream_and_collect(messages, "deepseek_v4_flash", None, None)
        assert seen["total"] > 250_000                                    # HTTP backend: untouched


@pytest.mark.asyncio
async def test_tool_loop_applies_the_backend_cap_too():
    from types import SimpleNamespace

    from core.nodes import execute

    captured: dict = {}

    class _Bound:
        async def ainvoke(self, lc_messages):
            captured["chars"] = sum(len(str(m.content)) for m in lc_messages)
            return SimpleNamespace(content="done", tool_calls=[])

    class _Model:
        def bind_tools(self, tools):
            return _Bound()

    async def fake_tools(names):
        return [SimpleNamespace(name="get_server_time")]

    messages = [{"role": "system", "content": _prior(300, 1_000)}, {"role": "user", "content": "q"}]
    with patch.object(execute, "get_all_tools", fake_tools), \
            patch.object(execute, "_build_tool_model", lambda backend: _Model()):
        assert await execute._run_tool_loop(messages, "kimi_cli", "assistant-tools", None) == "done"
        assert captured["chars"] <= 100_000
        await execute._run_tool_loop(messages, "deepseek_v4_flash", "assistant-tools", None)
        assert captured["chars"] > 250_000


# ── turn events carry conversation + owner ───────────────────────────────────

@pytest.fixture
def _memory_on():
    original = config.MEMORY_ENABLED
    config.MEMORY_ENABLED = True
    yield
    config.MEMORY_ENABLED = original


def _memory(append: AsyncMock):
    singletons = MagicMock()
    singletons.event_log.atomic_append = append
    return patch("core.memory.bootstrap.get_memory", return_value=singletons)


@pytest.mark.asyncio
async def test_turn_event_is_stamped_with_conversation_owner_and_history_fit(_memory_on):
    append = AsyncMock()
    fit = {"backend": "deepseek_v4_flash", "messages_in": 9, "messages_kept": 9, "messages_omitted": 0}
    with _memory(append):
        await _append_agent_turn_event(
            {"messages": [{"role": "user", "content": "q"}], "face_id": "assistant",
             "conversation_id": "conv-1", "user_id": "dad", "history_fit": fit},
            assistant_response="a",
        )
    body = append.call_args[0][0]
    assert body["conversation_id"] == "conv-1" and body["user_id"] == "dad" and body["history"] == fit


@pytest.mark.asyncio
async def test_turn_event_stamp_defaults_to_none(_memory_on):
    append = AsyncMock()
    with _memory(append):
        await _append_agent_turn_event({"messages": [{"role": "user", "content": "q"}]}, assistant_response="a")
    body = append.call_args[0][0]
    assert body["conversation_id"] is None and body["user_id"] is None and body["history"] is None
