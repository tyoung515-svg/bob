"""Claims compactor: region selection, parsing, the deterministic validator, idempotence, shadow trigger."""
from __future__ import annotations

import json

import pytest

from core.context import compactor, store
from core.context.budget import estimate_tokens
from core.context.compactor import (
    CompactionParseError,
    compact_region,
    extract_literals,
    parse_response,
    run_shadow,
    select_region,
    soft_trigger_tokens,
    validate_claims,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "ctx.db"))
    for key in ("BOB_COMPACTOR_SHADOW", "BOB_COMPACTOR_SOFT_TOKENS", "BOB_CTX_PROFILE", "BOB_CONTEXT_FIT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    compactor._LOCKS.clear()
    compactor._COOLDOWN.clear()
    compactor._TASKS.clear()


def _row(i, role=None, content=None):
    role = role or ("user" if i % 2 == 0 else "assistant")
    content = content if content is not None else f"message {i} " + "x" * 400
    return {"message_id": f"m{i}", "role": role, "content": content,
            "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00", "content_sha256": store.sha256_text(content)}


def _conv(n, size=400):
    return [_row(i, content=f"message {i} " + "x" * size) for i in range(n)]


def _tok(m):
    return estimate_tokens(m["content"]) + 4


# ── select_region ────────────────────────────────────────────────────────────

def test_region_is_whole_exchanges_between_the_opening_and_the_protected_tail():
    msgs = _conv(40, size=1000)
    region = select_region(msgs, tail_tokens=2_000, min_region_tokens=1_000, max_region_tokens=1_000_000)
    assert region[0]["message_id"] == "m2" and region[0]["role"] == "user"        # m0, m1 are protected
    assert region[-1]["role"] == "assistant"
    tail = msgs[int(region[-1]["message_id"][1:]) + 1:]
    assert tail[0]["role"] == "user" and sum(_tok(m) for m in tail) >= 2_000


def test_no_region_when_too_short_or_too_small():
    assert select_region(_conv(4)) is None
    assert select_region(_conv(40, size=10), tail_tokens=50, min_region_tokens=50_000) is None


def test_region_size_is_capped_from_the_oldest_side_without_splitting_an_exchange():
    region = select_region(_conv(60, size=1000), tail_tokens=2_000, min_region_tokens=100, max_region_tokens=3_000)
    assert region[0]["message_id"] == "m2" and region[-1]["role"] == "assistant" and len(region) % 2 == 0
    assert 4 <= len(region) <= 10


def test_an_oversized_first_exchange_is_taken_whole():
    msgs = _conv(20, size=100)
    msgs[2], msgs[3] = _row(2, content="U" * 30_000), _row(3, content="A" * 30_000)
    region = select_region(msgs, tail_tokens=500, min_region_tokens=100, max_region_tokens=1_000)
    assert [m["message_id"] for m in region] == ["m2", "m3"]


def test_a_huge_last_message_still_leaves_a_protected_tail():
    msgs = _conv(30, size=200)
    msgs[-1] = _row(29, content="Z" * 60_000)
    region = select_region(msgs, tail_tokens=1_000, min_region_tokens=100)
    assert int(region[-1]["message_id"][1:]) <= 27                                  # the last exchange stays out


# ── parse_response ───────────────────────────────────────────────────────────

def test_parse_accepts_fenced_json_prose_wrapped_json_and_string_narratives():
    claims, narrative = parse_response('```json\n{"claims":[{"id":"c1"}],"narrative":{"goal":"g"}}\n```')
    assert claims == [{"id": "c1"}] and narrative == {"goal": "g"}
    claims, narrative = parse_response('Sure! {"claims":[],"narrative":"all good"} thanks')
    assert claims == [] and narrative == {"summary": "all good"}


def test_parse_rejects_unusable_answers():
    for bad in ("no json here", '{"claims": "nope"}', '{"claims": [}', ""):
        with pytest.raises(CompactionParseError):
            parse_response(bad)


# ── validation ───────────────────────────────────────────────────────────────

def test_extract_literals_finds_hard_values_and_ignores_single_digits():
    found = extract_literals(
        'Use v0.99.0 at https://x.io/a on 2026-10-23, path ~/bob/core/app.py, id 3f2a9c1b7d, '
        '1,500 users at 50%, retry 3 times, key `FOO_BAR`, said "keep it"')
    values = {lit for _kind, lit in found}
    assert {"v0.99.0", "https://x.io/a", "2026-10-23", "~/bob/core/app.py", "3f2a9c1b7d",
            "1,500", "50%", "FOO_BAR", "keep it"} <= values
    assert "3" not in values


REGION = [
    _row(2, content="We decided the deploy target is staging-eu-2 and the limit is 4,000 requests. See https://ex.io/spec"),
    _row(3, role="assistant", content="Noted: limit 4000 requests; I will use /srv/app/config.yaml"),
    _row(4, content="Correction: the limit is 5,500 requests, not 4,000."),
]


def _claim(cid, ctype, text, cites, supersedes=None):
    return {"id": cid, "type": ctype, "text": text, "cites": cites, "supersedes": supersedes}


def _reasons(report):
    return {r["id"]: r["reason"] for r in report["rejected"]}


def test_good_claims_are_accepted_and_numbers_ignore_thousands_separators():
    claims, report = validate_claims([
        _claim("c1", "decision", "Deploy target is staging-eu-2", ["m2"]),
        _claim("c2", "constraint", "Limit is 4000 requests", ["m2"]),
        _claim("c3", "artifact", "Spec at https://ex.io/spec", ["m2"]),
        _claim("c4", "fact", "Config lives in /srv/app/config.yaml", ["m3"]),
    ], REGION)
    assert [c["id"] for c in claims] == ["c1", "c2", "c3", "c4"] and not report["rejected"]
    assert all(c["status"] == "VERIFIED" for c in claims)


def test_planted_errors_are_rejected():
    _claims, report = validate_claims([
        _claim("c1", "constraint", "Limit is 4,500 requests", ["m2"]),               # wrong number
        _claim("c2", "artifact", "Spec at https://ex.io/other", ["m2"]),             # URL not in the source
        _claim("c3", "fact", "Config in /srv/app/config.yaml", ["m2"]),              # path is in m3, not m2
        _claim("c4", "fact", "Something", ["m99"]),                                  # cite outside the region
        _claim("c5", "fact", "Something", []),                                       # no cites
        _claim("c6", "gossip", "Something", ["m2"]),                                 # bad type
        _claim("c7", "fact", "   ", ["m2"]),                                         # empty
        _claim("c8", "fact", "x" * 700, ["m2"]),                                     # too long
        "not an object",
    ], REGION)
    reasons = _reasons(report)
    assert reasons["c1"] == "literal_unsupported:4,500" and reasons["c2"].startswith("literal_unsupported:https://ex.io/other")
    assert reasons["c3"].startswith("literal_unsupported:") and reasons["c4"] == "cite_outside_region:m99"
    assert reasons["c5"] == "no_cites" and reasons["c6"] == "bad_type:gossip"
    assert reasons["c7"] == "empty_text" and reasons["c8"] == "text_too_long" and reasons["#9"] == "not_an_object"
    assert report["claims_ok"] == 0 and report["literals_unsupported"] >= 3


def test_duplicate_ids_and_duplicate_text_are_rejected():
    claims, report = validate_claims([
        _claim("c1", "decision", "Deploy target is staging-eu-2", ["m2"]),
        _claim("c1", "decision", "Another thing", ["m2"]),
        _claim("c3", "decision", "deploy target is  staging-eu-2", ["m2"]),
    ], REGION)
    assert [c["id"] for c in claims] == ["c1"]
    assert _reasons(report) == {"c1": "duplicate_id", "c3": "duplicate_text"}


def test_supersession_is_kept_when_valid_and_dropped_with_a_warning_when_not():
    claims, report = validate_claims([
        _claim("c1", "constraint", "Limit is 4,000 requests", ["m2"]),
        _claim("c2", "correction", "Limit is 5,500 requests", ["m4"], supersedes="c1"),
        _claim("c3", "fact", "Spec at https://ex.io/spec", ["m2"], supersedes="c99"),
    ], REGION)
    by_id = {c["id"]: c for c in claims}
    assert by_id["c2"]["supersedes"] == "c1" and by_id["c3"]["supersedes"] is None
    assert len(report["warnings"]) == 1 and "c99" in report["warnings"][0]


def test_coverage_and_gaps_are_reported():
    _claims, report = validate_claims([
        _claim("c1", "decision", "Deploy target is staging-eu-2", ["m2"]),
        _claim("c2", "correction", "Limit is 5,500 requests", ["m4"]),
    ], REGION)
    assert report["covered_messages"] == 2 and report["coverage"] == 0.667
    assert report["gaps"] == ["m3"] and report["gap_count"] == 1


# ── compact_region ───────────────────────────────────────────────────────────

GOOD = json.dumps({
    "claims": [_claim("c1", "decision", "Deploy target is staging-eu-2", ["m2"])],
    "narrative": {"goal": "ship it"},
})
BAD = json.dumps({"claims": [_claim("c1", "fact", "Made up", ["m99"])], "narrative": {}})


def _llm(*answers):
    calls = []

    async def call(messages):
        calls.append(messages)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    call.backend = "fake"
    call.calls = calls
    return call


async def test_compaction_is_stored_and_idempotent():
    llm = _llm(GOOD)
    first = await compact_region("c1", "dad", REGION, llm)
    assert first.status == "shadow_ok" and first.attempts == 1 and not first.cached
    assert first.claims[0]["text"] == "Deploy target is staging-eu-2" and first.narrative == {"goal": "ship it"}
    stored = store.get_compaction(first.compaction_id)
    assert stored["status"] == "shadow_ok" and stored["region_first_id"] == "m2" and stored["region_last_id"] == "m4"
    second = await compact_region("c1", "dad", REGION, llm)
    assert second.cached and second.claims == first.claims and len(llm.calls) == 1


async def test_the_prompt_carries_the_ids_and_the_rules():
    llm = _llm(GOOD)
    await compact_region("c1", "dad", REGION, llm)
    system, user = llm.calls[0]
    assert "verbatim" in system["content"].lower() and "### m2 | user |" in user["content"]
    assert "staging-eu-2" in user["content"]


async def test_an_unusable_answer_is_retried_with_the_problem_fed_back():
    llm = _llm("not json at all", GOOD)
    result = await compact_region("c1", "dad", REGION, llm)
    assert result.status == "shadow_ok" and result.attempts == 2
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" in llm.calls[1][1]["content"]


async def test_claims_that_fail_validation_are_rejected_after_a_retry_and_the_reasons_are_fed_back():
    llm = _llm(BAD, BAD)
    result = await compact_region("c1", "dad", REGION, llm)
    assert result.status == "shadow_rejected" and result.claims == [] and result.attempts == 2
    assert "cite_outside_region:m99" in llm.calls[1][1]["content"]
    assert (await compact_region("c1", "dad", REGION, llm)).cached                    # rejected is cached too


async def test_a_provider_error_is_a_failed_result_not_an_exception_and_is_not_cached():
    llm = _llm(RuntimeError("boom"))
    result = await compact_region("c1", "dad", REGION, llm)
    assert result.status == "failed" and result.error.startswith("llm: RuntimeError") and result.claims == []
    assert store.get_compaction(result.compaction_id)["status"] == "failed"
    again = await compact_region("c1", "dad", REGION, _llm(GOOD))
    assert again.status == "shadow_ok" and not again.cached


async def test_mostly_rejected_claims_fail_the_region_but_a_few_bad_ones_do_not():
    good = _claim("c1", "decision", "Deploy target is staging-eu-2", ["m2"])
    bad = [_claim(f"x{i}", "fact", "Nope", ["m99"]) for i in range(2)]
    mostly_bad = json.dumps({"claims": [good, *bad], "narrative": {}})
    assert (await compact_region("c1", "dad", REGION, _llm(mostly_bad, mostly_bad))).status == "shadow_rejected"
    one_bad = json.dumps({"claims": [good, _claim("c2", "constraint", "Limit is 5,500 requests", ["m4"]), bad[0]], "narrative": {}})
    result = await compact_region("c2", "dad", REGION, _llm(one_bad))
    assert result.status == "shadow_ok" and len(result.claims) == 2


async def test_nothing_worth_keeping_is_a_valid_empty_compaction():
    result = await compact_region("c1", "dad", REGION, _llm('{"claims": [], "narrative": {}}'))
    assert result.status == "shadow_ok" and result.claims == [] and result.report["gap_count"] == 3


# ── trigger and run ──────────────────────────────────────────────────────────

def test_soft_trigger_levels(monkeypatch):
    assert soft_trigger_tokens(1_000_000) == 200_000          # 25% of the window, capped at 200K tokens
    assert soft_trigger_tokens(400_000) == 100_000
    assert soft_trigger_tokens(131_072) == 32_768
    monkeypatch.setenv("BOB_CTX_PROFILE", "long")
    assert soft_trigger_tokens(1_000_000) == 700_000
    monkeypatch.setenv("BOB_COMPACTOR_SOFT_TOKENS", "123456")
    assert soft_trigger_tokens(1_000_000) == 123_456


def test_scheduling_needs_a_running_loop():
    state = {"conversation_id": "c1", "user_id": "dad",
             "history_fit": {"backend": "deepseek_v4_flash", "window_tokens": 1_000_000, "tokens_est_before": 900_000}}
    assert compactor.maybe_schedule_shadow(state) is False


def _conversation_in_store(n=40, size=2_000):
    store.record_history("c1", "dad", [
        {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant",
         "content": f"message {i} " + "x" * size, "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00"}
        for i in range(n)
    ])


async def test_a_run_compacts_the_oldest_closed_region_and_a_second_run_reuses_it(monkeypatch):
    monkeypatch.setattr(compactor, "MAX_REGIONS_PER_RUN", 1)
    _conversation_in_store()
    llm = _llm(json.dumps({"claims": [_claim("c1", "fact", "Opening topic noted", ["m2"])], "narrative": {}}))
    results = await run_shadow("c1", "dad", 100_000, llm)
    assert [r.status for r in results] == ["shadow_ok"] and results[0].report["region_messages"] >= 4
    assert results[0].claims[0]["id"] == "r1.c1"                                         # ids are namespaced per region
    assert store.list_compactions("c1")[0]["status"] == "shadow_ok" and len(llm.calls) == 1


async def test_a_run_does_nothing_for_a_short_conversation():
    _conversation_in_store(n=4)
    llm = _llm(GOOD)
    assert await run_shadow("c1", "dad", 100_000, llm) == [] and llm.calls == []
