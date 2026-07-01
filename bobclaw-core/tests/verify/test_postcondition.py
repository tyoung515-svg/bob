"""Unit tests for core.verify.postcondition (§2.6 tier-1 post-condition critic).

PURE — no network (pytest runs --disable-socket). The critic call is always driven by an
INJECTED fake ``async def send(messages, backend)``; a real backend is never touched.
"""
from __future__ import annotations

import json

import pytest

from core.verify.postcondition import (
    DEFAULT_CRITIC_PREFERENCE,
    FAMILY_BY_BACKEND,
    PCVerdict,
    PostConditionError,
    PostConditionResult,
    build_pc_prompt,
    decorrelated_critic_backend,
    family_of,
    is_decorrelated,
    make_postcondition_verifier,
    parse_pc_verdict,
    PC_PROMPT_TEMPLATE,
    verify_post_condition,
)
from core.ses.falsepass import false_pass_rate
from core.ses.types import Label, LabeledItem


# ── fake critic transports (canned replies; never the network) ──────────────────
async def _send_holds(messages, backend):
    return json.dumps({"verdict": "holds", "reasons": ["satisfies the declared condition"]})


async def _send_violated(messages, backend):
    return json.dumps({"verdict": "violated", "reasons": ["output changed but condition unmet"]})


async def _send_unknown(messages, backend):
    return json.dumps({"verdict": "unknown", "reasons": []})


async def _send_raises(messages, backend):
    raise ConnectionError("simulated backend failure")


# ── family taxonomy + decorrelation ─────────────────────────────────────────────
def test_family_of_mapped():
    assert family_of("deepseek_v4_flash") == "deepseek"
    assert family_of("glm_5_2") == "glm"
    assert family_of("claude_code") == "claude"
    assert family_of("claude_api") == "claude"
    assert family_of("agy_code") == "gemini"
    assert family_of("kimi_cli") == "kimi"


def test_family_of_unmapped_is_own_string():
    assert family_of("brand_new_backend") == "brand_new_backend"
    assert family_of("") == "unknown"


def test_is_decorrelated():
    assert is_decorrelated("deepseek_v4_flash", "glm_5_2") is True
    assert is_decorrelated("claude_api", "claude_code") is False  # same family
    assert is_decorrelated("deepseek_v4_flash", "deepseek_v4_flash") is False


def test_unmapped_backend_decorrelation_is_documented():
    # DOCUMENTED behavior (audit r3 §2): an unmapped backend is its OWN family, so two distinct
    # unmapped strings read as decorrelated (we cannot know two unknown aliases are the same model
    # without an alias map). The SAME string is never decorrelated from itself.
    assert is_decorrelated("custom_a", "custom_b") is True
    assert is_decorrelated("custom_a", "custom_a") is False


def test_resolver_only_returns_mapped_critic_for_unmapped_actor():
    # The resolver only ever hands back a backend from the MAPPED preference, so a
    # "both-unmapped" actor/critic pair can never arise from decorrelated_critic_backend.
    crit = decorrelated_critic_backend("some_unmapped_actor_backend")
    assert crit in FAMILY_BY_BACKEND
    assert is_decorrelated("some_unmapped_actor_backend", crit) is True


def test_default_preference_spans_six_families():
    fams = {family_of(b) for b in DEFAULT_CRITIC_PREFERENCE}
    assert len(fams) >= 6
    # every entry is a real registered backend
    from core.config import KNOWN_BACKENDS

    assert all(b in KNOWN_BACKENDS for b in DEFAULT_CRITIC_PREFERENCE)


def test_decorrelated_critic_default_cross_family():
    # THE decorrelation property: actor=deepseek => critic family != deepseek.
    crit = decorrelated_critic_backend("deepseek_v4_flash")
    assert family_of(crit) != "deepseek"
    crit_glm = decorrelated_critic_backend("glm_5_2")
    assert family_of(crit_glm) != "glm"


def test_decorrelated_critic_team_cross_family_chosen(monkeypatch):
    # A team whose critic role is a DIFFERENT family than the actor is honored (JOAT reuse).
    monkeypatch.setattr(
        "core.teams.role_backend",
        lambda team, role: "deepseek_v4_flash" if role == "critic" else None,
    )
    crit = decorrelated_critic_backend("glm_5_2", team="t")  # actor glm, team critic deepseek
    assert crit == "deepseek_v4_flash"


def test_decorrelated_critic_team_same_family_skipped(monkeypatch):
    # A team critic in the SAME family as the actor is skipped → cross-family default.
    monkeypatch.setattr(
        "core.teams.role_backend",
        lambda team, role: "deepseek_v4_flash" if role == "critic" else None,
    )
    crit = decorrelated_critic_backend("deepseek_v4_flash", team="t")
    assert family_of(crit) != "deepseek"


def test_decorrelated_critic_candidates_in_order():
    crit = decorrelated_critic_backend(
        "deepseek_v4_flash", candidates=["deepseek_v4_flash", "glm_5_2", "claude_api"]
    )
    assert crit == "glm_5_2"  # first cross-family in the candidate list


def test_decorrelated_critic_none_raises(monkeypatch):
    # With the default preference emptied (monkeypatch auto-restores; no flaky module mutation)
    # and an all-same-family candidate pool, no cross-family critic exists → PostConditionError.
    monkeypatch.setattr("core.verify.postcondition.DEFAULT_CRITIC_PREFERENCE", ())
    with pytest.raises(PostConditionError):
        decorrelated_critic_backend("deepseek_v4_flash", candidates=["deepseek_v4_flash"])


# ── parse_pc_verdict ────────────────────────────────────────────────────────────
def test_parse_bare_json():
    v, r = parse_pc_verdict('{"verdict": "holds", "reasons": ["ok"]}')
    assert v is PCVerdict.HOLDS and r == ["ok"]


def test_parse_fenced_json():
    raw = "```json\n{\"verdict\": \"violated\", \"reasons\": [\"no\"]}\n```"
    v, r = parse_pc_verdict(raw)
    assert v is PCVerdict.VIOLATED and r == ["no"]


def test_parse_embedded_json():
    raw = 'Here is my verdict: {"verdict":"unknown","reasons":[]} thanks'
    v, r = parse_pc_verdict(raw)
    assert v is PCVerdict.UNKNOWN


def test_parse_garbage_is_unknown():
    v, r = parse_pc_verdict("the post-condition definitely holds, trust me")
    assert v is PCVerdict.UNKNOWN
    assert any("parse_error" in x for x in r)


def test_parse_unknown_verdict_string_is_unknown():
    v, r = parse_pc_verdict('{"verdict": "maybe", "reasons": ["x"]}')
    assert v is PCVerdict.UNKNOWN
    assert any("parse_error" in x for x in r)


def test_parse_schema_echo_cannot_false_pass():
    # If a lazy critic echoes the prompt's JSON SCHEMA verbatim, it is invalid JSON (the
    # holds|violated|unknown pipe-union) → UNKNOWN → not-passed (fail-safe, never a false pass).
    echoed = '{"verdict":"holds"|"violated"|"unknown","reasons":["short reason", "..."]}'
    v, _ = parse_pc_verdict(echoed)
    assert v is PCVerdict.UNKNOWN


# ── verify_post_condition ───────────────────────────────────────────────────────
async def test_verify_holds_passes():
    res = await verify_post_condition(
        step="rename report.txt", statement="report_final.txt exists and report.txt is gone",
        result="dir now has report_final.txt; report.txt gone", actor_backend="deepseek_v4_flash",
        send=_send_holds,
    )
    assert res.verdict is PCVerdict.HOLDS
    assert res.passed is True
    assert res.decorrelated is True
    assert family_of(res.critic_backend) != "deepseek"  # decorrelated


async def test_verify_violated_does_not_pass():
    res = await verify_post_condition(
        step="close account", statement="balance is $0 and account closed",
        result="balance still $4521.77, status ACTIVE", actor_backend="deepseek_v4_flash",
        send=_send_violated,
    )
    assert res.verdict is PCVerdict.VIOLATED
    assert res.passed is False


async def test_verify_unknown_does_not_pass():
    res = await verify_post_condition(
        step="s", statement="x", result="y", actor_backend="deepseek_v4_flash", send=_send_unknown,
    )
    assert res.passed is False


async def test_verify_critic_failure_is_failsafe():
    res = await verify_post_condition(
        step="s", statement="x", result="y", actor_backend="deepseek_v4_flash", send=_send_raises,
    )
    assert res.verdict is PCVerdict.UNKNOWN
    assert res.passed is False
    assert any("critic_unavailable" in r for r in res.reasons)


async def test_verify_same_family_override_rejected():
    with pytest.raises(PostConditionError, match="decorrelated"):
        await verify_post_condition(
            step="s", statement="x", result="y", actor_backend="claude_api",
            critic_backend="claude_code", send=_send_holds,  # same family
        )


async def test_verify_empty_statement_is_failsafe_no_critic_call():
    # Core fail-safe: an empty/whitespace statement is refused WITHOUT calling the critic.
    calls = []

    async def _track(messages, backend):
        calls.append(backend)
        return '{"verdict": "holds", "reasons": []}'

    for blank in ("", "   ", "\n"):
        res = await verify_post_condition(step="s", statement=blank, result="r",
                                          actor_backend="deepseek_v4_flash", send=_track)
        assert res.passed is False
        assert res.verdict is PCVerdict.UNKNOWN
        assert "no post-condition declared" in res.reasons
    assert calls == []  # the critic was never called for a blank post-condition


def test_verifier_resolution_error_is_failsafe_false():
    # A verifier whose fixed critic_backend collides with the actor's family raises
    # PostConditionError inside verify → the verifier converts it to False (never auto-pass,
    # never aborts the false_pass_rate measurement).
    async def _holds(messages, backend):
        return '{"verdict": "holds", "reasons": []}'

    verifier = make_postcondition_verifier(send=_holds, critic_backend="deepseek_v4_flash",
                                           default_actor_backend="deepseek_v4_flash")  # same family
    assert verifier({"step": "s", "statement": "x", "result": "y"}) is False


async def test_verify_explicit_cross_family_override_ok():
    res = await verify_post_condition(
        step="s", statement="x", result="y", actor_backend="deepseek_v4_flash",
        critic_backend="glm_5_2", send=_send_holds,
    )
    assert res.passed is True
    assert res.critic_backend == "glm_5_2"


async def test_verify_prompt_carries_step_statement_result_and_no_label():
    seen = {}

    async def _capture(messages, backend):
        seen["messages"] = messages
        return json.dumps({"verdict": "holds", "reasons": []})

    await verify_post_condition(
        step="STEP_TOKEN", statement="STATEMENT_TOKEN", result="RESULT_TOKEN",
        actor_backend="deepseek_v4_flash", send=_capture,
    )
    user = next(m["content"] for m in seen["messages"] if m["role"] == "user")
    assert "STEP_TOKEN" in user
    assert "STATEMENT_TOKEN" in user
    assert "RESULT_TOKEN" in user
    # never leak a ground-truth label to the critic
    assert "label" not in user.lower()


def test_prompt_template_instructs_not_pass_on_mere_output_change():
    low = PC_PROMPT_TEMPLATE.lower()
    assert "changed" in low and ("violation" in low or "not pass" in low or "do not pass" in low)


def test_build_pc_prompt_includes_fields():
    p = build_pc_prompt("STEP_X", "STMT_Y", "RES_Z")
    assert "STEP_X" in p and "STMT_Y" in p and "RES_Z" in p
    # brace-safe rendering leaves the JSON example single-braced (no leftover {{ from .format)
    assert '{"verdict"' in p and "{{" not in p


async def test_verify_handles_literal_braces_in_content():
    # A post-condition about JSON/code carries literal { } braces. str.format does NOT recurse
    # into substituted values, so this must NOT raise (regression lock for the audit r1 concern).
    seen = {}

    async def _capture(messages, backend):
        seen["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return '{"verdict": "holds", "reasons": []}'

    res = await verify_post_condition(
        step='call f({"a": 1})',
        statement='the response body equals {"ok": true} exactly',
        result='returned {"ok": true} with status {200}',
        actor_backend="deepseek_v4_flash", send=_capture,
    )
    assert res.passed is True
    assert '{"ok": true}' in seen["user"]  # braces survived verbatim into the prompt


async def test_make_verifier_is_event_loop_safe():
    # Calling the SYNC verifier from inside a running loop must NOT raise (thread fallback).
    async def _holds(messages, backend):
        return '{"verdict": "holds", "reasons": []}'

    verifier = make_postcondition_verifier(send=_holds, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    # this test function itself runs inside an event loop (asyncio_mode=auto)
    assert verifier({"step": "s", "statement": "x", "result": "y"}) is True


# ── MS-5 integration: the node is a valid false_pass_rate verifier ──────────────
# A SATISFIED post-condition carries "SATISFIED" in its result; an unmet one carries "FAILED".
def _planted_set():
    return [
        LabeledItem("t1", {"step": "s", "statement": "x", "result": "SATISFIED",
                           "actor_backend": "deepseek_v4_flash"}, Label.TRUE),
        LabeledItem("t2", {"step": "s", "statement": "x", "result": "SATISFIED",
                           "actor_backend": "deepseek_v4_flash"}, Label.TRUE),
        LabeledItem("w1", {"step": "s", "statement": "x", "result": "FAILED EDGE",
                           "actor_backend": "deepseek_v4_flash"}, Label.WRONG),
        LabeledItem("w2", {"step": "s", "statement": "x", "result": "FAILED",
                           "actor_backend": "deepseek_v4_flash"}, Label.WRONG),
        LabeledItem("w3", {"step": "s", "statement": "x", "result": "FAILED",
                           "actor_backend": "deepseek_v4_flash"}, Label.WRONG),
    ]


def test_falsepass_perfect_verifier_zero():
    async def perfect(messages, backend):
        content = next(m["content"] for m in messages if m["role"] == "user")
        verdict = "holds" if "SATISFIED" in content else "violated"
        return json.dumps({"verdict": verdict, "reasons": []})

    verifier = make_postcondition_verifier(send=perfect, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    out = false_pass_rate(_planted_set(), verifier)
    assert out["false_pass_rate"] == 0.0
    assert out["wrong_caught"] == 3
    assert out["true_passed"] == 2


def test_falsepass_blind_verifier_one():
    async def blind(messages, backend):
        return json.dumps({"verdict": "holds", "reasons": []})

    verifier = make_postcondition_verifier(send=blind, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    out = false_pass_rate(_planted_set(), verifier)
    assert out["false_pass_rate"] == 1.0
    assert out["wrong_passed"] == 3
    assert out["false_pass_ids"] == ["w1", "w2", "w3"]


def test_falsepass_partial_verifier_exact_fraction():
    async def partial(messages, backend):
        content = next(m["content"] for m in messages if m["role"] == "user")
        verdict = "holds" if ("SATISFIED" in content or "EDGE" in content) else "violated"
        return json.dumps({"verdict": verdict, "reasons": []})

    verifier = make_postcondition_verifier(send=partial, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    out = false_pass_rate(_planted_set(), verifier)
    assert abs(out["false_pass_rate"] - 1 / 3) < 1e-9
    assert out["false_pass_ids"] == ["w1"]


def test_verifier_drops_a_stray_label_key_from_the_prompt():
    # Adversarial: even if a payload accidentally carries a ground-truth "label" key, the verifier
    # only reads step/statement/result — the label value must NEVER reach the critic prompt.
    seen = {}

    async def _capture(messages, backend):
        seen["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return '{"verdict": "holds", "reasons": []}'

    verifier = make_postcondition_verifier(send=_capture, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    verifier({"step": "s", "statement": "x", "result": "r", "label": "WRONG_LEAK_TOKEN"})
    assert "WRONG_LEAK_TOKEN" not in seen["user"]


def test_falsepass_verifier_never_sees_a_label():
    seen_contents = []

    async def capture(messages, backend):
        seen_contents.append(next(m["content"] for m in messages if m["role"] == "user"))
        return json.dumps({"verdict": "holds", "reasons": []})

    verifier = make_postcondition_verifier(send=capture, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    false_pass_rate(_planted_set(), verifier)
    assert len(seen_contents) == 5  # one critic call per item


def test_verifier_accepts_alt_post_condition_key():
    # The verifier reads the spec-alt "post_condition" key as the declaration (not just "statement").
    seen = {}

    async def _capture(messages, backend):
        seen["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return '{"verdict": "holds", "reasons": []}'

    verifier = make_postcondition_verifier(send=_capture, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    assert verifier({"step": "s", "post_condition": "ALT_KEY_STMT", "result": "r"}) is True
    assert "ALT_KEY_STMT" in seen["user"]


def test_verifier_empty_statement_returns_false_no_critic_call():
    """Regression guard: a verifier built for false_pass_rate must not let a lenient
    critic 'holds' an empty statement, which would create a false pass."""
    calls = []

    async def never_called(messages, backend):
        calls.append(backend)
        return json.dumps({"verdict": "holds", "reasons": []})

    verifier = make_postcondition_verifier(send=never_called, critic_backend="glm_5_2",
                                           default_actor_backend="deepseek_v4_flash")
    assert verifier({"step": "s", "result": "r"}) is False
    assert verifier({"step": "s", "statement": "", "result": "r"}) is False
    assert verifier({"step": "s", "post_condition": "", "result": "r"}) is False
    assert calls == []
