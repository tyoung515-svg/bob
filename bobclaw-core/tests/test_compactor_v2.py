"""Claims-v2: importance ranking, region-namespaced ids, cross-region supersession, prior-claim aware prompts."""
from __future__ import annotations

import json

import pytest

from core.context import compactor, store
from core.context.compactor import (
    CompactionResult,
    active_claims,
    build_prompt,
    compact_region,
    default_prompt_version,
    effective_importance,
    extract_literals,
    render_claims_block,
    validate_claims,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "ctx.db"))
    for key in ("BOB_COMPACTOR_SHADOW", "BOB_COMPACTOR_PROMPT", "BOB_CTX_PROFILE", "BOB_CONTEXT_FIT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    compactor._LOCKS.clear()
    compactor._COOLDOWN.clear()
    compactor._TASKS.clear()


def _row(i, content, role=None):
    role = role or ("user" if i % 2 == 0 else "assistant")
    return {"message_id": f"m{i}", "role": role, "content": content,
            "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00", "content_sha256": store.sha256_text(content)}


# region 1 states the freeze date; region 2 (later) corrects it
REGION1 = [
    _row(2, "The freeze starts 2026-12-15. The deploy target is staging-eu-2."),
    _row(3, "Noted: freeze 2026-12-15, target staging-eu-2.", "assistant"),
]
REGION2 = [
    _row(10, "Correction: the freeze starts 2026-12-18 instead."),
    _row(11, "Updated: freeze 2026-12-18. Priya owns the rollback plan.", "assistant"),
]


def _claim(cid, ctype, text, cites, supersedes=None, importance=None):
    c = {"id": cid, "type": ctype, "text": text, "cites": cites, "supersedes": supersedes}
    if importance is not None:
        c["importance"] = importance
    return c


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


def _result(claims, region, status="shadow_ok", narrative=None):
    return CompactionResult(
        compaction_id=f"id{region[0]['message_id']}", status=status, claims=claims, narrative=narrative or {},
        region_first_id=region[0]["message_id"], region_last_id=region[-1]["message_id"], region_count=len(region),
    )


# ── prompt versions ──────────────────────────────────────────────────────────

def test_default_prompt_version_is_v2_and_the_env_can_pin_v1_but_not_garbage(monkeypatch):
    assert default_prompt_version() == "claims-v2"
    monkeypatch.setenv("BOB_COMPACTOR_PROMPT", "claims-v1")
    assert default_prompt_version() == "claims-v1"
    monkeypatch.setenv("BOB_COMPACTOR_PROMPT", "claims-v9")
    assert default_prompt_version() == "claims-v2"


def test_v2_prompt_ranks_by_importance_and_v1_does_not():
    v2 = build_prompt(REGION1, version="claims-v2")[0]["content"]
    v1 = build_prompt(REGION1, version="claims-v1")[0]["content"]
    assert '"importance"' in v2 and "routine status updates" in v2 and "ALREADY-KNOWN" in v2
    assert "importance" not in v1.lower()


def test_prior_claims_reach_only_the_v2_prompt_and_with_their_ids():
    prior = [{"id": "r1.c1", "type": "constraint", "text": "Freeze starts 2026-12-15"}]
    v2 = build_prompt(REGION2, prior_claims=prior, version="claims-v2")[1]["content"]
    v1 = build_prompt(REGION2, prior_claims=prior, version="claims-v1")[1]["content"]
    assert v2.startswith("ALREADY-KNOWN CLAIMS") and "- r1.c1 [constraint] Freeze starts 2026-12-15" in v2
    assert v2.index("ALREADY-KNOWN") < v2.index("CONVERSATION REGION")
    assert "r1.c1" not in v1
    assert "ALREADY-KNOWN" not in build_prompt(REGION2, version="claims-v2")[1]["content"]


def test_max_claims_is_stated_in_the_prompt():
    assert "At most 25 claims" in build_prompt(REGION1, version="claims-v2", max_claims=25)[0]["content"]


# ── importance ───────────────────────────────────────────────────────────────

def test_importance_is_kept_when_valid_and_defaults_to_2_otherwise():
    raw = [_claim("c1", "decision", "Target is staging-eu-2", ["m2"], importance=3),
           _claim("c2", "fact", "Freeze starts 2026-12-15", ["m2"], importance=1),
           _claim("c3", "fact", "Target noted staging-eu-2", ["m2"], importance="3"),
           _claim("c4", "fact", "Freeze noted 2026-12-15", ["m3"], importance=0),
           _claim("c5", "fact", "Freeze is 2026-12-15", ["m2"], importance=9),
           _claim("c6", "fact", "Freeze date 2026-12-15 noted", ["m2"], importance="high"),
           _claim("c7", "fact", "Freeze 2026-12-15 again", ["m2"])]
    claims, report = validate_claims(raw, REGION1)
    assert not report["rejected"]
    assert [c["importance"] for c in claims] == [3, 1, 3, 2, 2, 2, 2]


# ── numbers must match whole tokens ──────────────────────────────────────────

NUMS = [_row(2, "Ticket TRK-48213 was opened. Ports 8443,9443 are open; the limit is 12,400 requests, retry after 12 seconds. Ratio 3.14 held at 50%."),
        _row(3, "Noted.", "assistant")]


def _accepts(text):
    claims, report = validate_claims([_claim("c1", "fact", text, ["m2"])], NUMS)
    return bool(claims)


def test_a_number_is_not_found_inside_a_longer_number():
    assert not _accepts("Retry after 47 seconds")            # 47 is not a token anywhere
    assert not _accepts("The limit is 482 requests")         # 482 is only part of 48213
    assert not _accepts("The limit is 400 requests")         # 400 is only the tail of 12,400
    assert not _accepts("The limit is 2,400 requests")


def test_thousands_separators_spaced_lists_and_unspaced_lists_still_match():
    assert _accepts("The limit is 12400 requests") and _accepts("The limit is 12,400 requests")
    assert _accepts("Ports 8443 and 9443") and _accepts("Ports 8443,9443")
    assert not _accepts("Ports 8443,9444")
    assert _accepts("Retry after 12 seconds") and _accepts("Ratio 3.14 held at 50%")
    assert not _accepts("Ratio 3.15 held") and not _accepts("Held at 51%")


# ── sentence punctuation is not part of a path or URL ────────────────────────

def test_a_trailing_period_comma_or_colon_is_not_part_of_a_path_or_url_literal():
    values = {lit for _kind, lit in extract_literals("Config: /srv/app/conf.yaml. See https://ex.io/spec/v2, then ~/bob/run.sh;")}
    assert {"/srv/app/conf.yaml", "https://ex.io/spec/v2", "~/bob/run.sh"} <= values
    assert not any(v.endswith((".", ",", ";", ":")) for v in values)


def test_a_claim_that_ends_with_its_path_and_a_period_is_not_falsely_rejected():
    region = [_row(2, "The export lives at /srv/tracker/export/2026-q3.csv, see https://ex.io/spec/v2 (draft)"),
              _row(3, "Noted the export location.", "assistant")]
    claims, report = validate_claims([
        _claim("c1", "artifact", "Export path is /srv/tracker/export/2026-q3.csv.", ["m2"]),
        _claim("c2", "artifact", "Spec: https://ex.io/spec/v2.", ["m2"]),
    ], region)
    assert len(claims) == 2 and not report["rejected"]
    wrong, report = validate_claims([_claim("c1", "artifact", "Export path is /srv/tracker/export/2026-q4.csv.", ["m2"])], region)
    assert not wrong and report["rejected"][0]["reason"].startswith("literal_unsupported:/srv/tracker/export/2026-q4.csv")


# ── polarity: a negation in the source must survive into a prohibition ───────

NEG = [_row(2, "Do NOT enable the feature flag zeta-sync in production, it corrupts counts."),
       _row(3, "Understood, zeta-sync stays off.", "assistant"),
       _row(4, "Correction: the limit is 5,500 requests, not 4,000.")]


def _polarity(ctype, text, cites=("m2",)):
    claims, report = validate_claims([_claim("c1", ctype, text, list(cites))], NEG)
    return bool(claims), (report["rejected"][0]["reason"] if report["rejected"] else None)


def test_a_prohibition_that_loses_its_not_is_rejected_and_one_that_keeps_it_is_accepted():
    assert _polarity("constraint", "Enable the feature flag zeta-sync in production") == (False, "polarity_dropped:not")
    assert _polarity("constraint", "Do NOT enable zeta-sync in production")[0]
    assert _polarity("decision", "Feature flag zeta-sync is allowed in production")[0] is False


def test_equivalent_wordings_of_a_prohibition_are_accepted():
    for text in ("zeta-sync is forbidden in production", "zeta-sync must stay disabled in production",
                 "Avoid enabling zeta-sync in production", "zeta-sync can't be enabled in production", "Never enable zeta-sync"):
        assert _polarity("constraint", text)[0], text


def test_only_prohibition_like_types_are_checked_and_corrections_may_drop_the_not():
    assert _polarity("fact", "zeta-sync is enabled in production")[0]                    # not a polarity-bearing type
    assert _polarity("correction", "The limit is 5,500 requests", ("m4",))[0]            # 'not 4,000' is retired by supersedes
    assert _polarity("constraint", "The limit is 5,500 requests", ("m4",))[0]            # ', not 4,000' is a contrast, not a prohibition


def test_a_contrast_is_not_a_prohibition_but_a_prohibition_after_a_comma_still_is():
    region = [_row(2, "Pin libfoo to 3.14.2, not 3.15.0. The ticket is TRK-48213, not ours."),
              _row(3, "Pin libfoo to 3.14.2, do not move to 3.15.0, it breaks the exporter.", "assistant")]
    ok, _ = validate_claims([_claim("c1", "constraint", "libfoo is pinned to 3.14.2", ["m2"])], region)
    assert ok                                                                            # contrast only
    claims, report = validate_claims([_claim("c1", "constraint", "libfoo is pinned to 3.14.2 and may move to 3.15.0", ["m3"])], region)
    assert not claims and report["rejected"][0]["reason"] == "polarity_dropped:not"      # 'do not move' lost


def test_a_source_without_a_negation_never_triggers_the_rule_even_if_the_claim_has_one():
    region = [_row(2, "The deploy target is staging-eu-2."), _row(3, "ok", "assistant")]
    claims, _ = validate_claims([_claim("c1", "decision", "Do not deploy anywhere except staging-eu-2", ["m2"])], region)
    assert claims                                                                       # a paraphrase may add a negation


# ── namespaced ids and supersession ──────────────────────────────────────────

def test_a_prefix_namespaces_ids_and_in_response_supersedes():
    claims, report = validate_claims([
        _claim("c1", "constraint", "Freeze starts 2026-12-15", ["m2"]),
        _claim("c2", "correction", "Freeze starts 2026-12-18", ["m10"], supersedes="c1"),
    ], REGION1 + REGION2, prefix="r2")
    assert [c["id"] for c in claims] == ["r2.c1", "r2.c2"]
    assert claims[1]["supersedes"] == "r2.c1" and not report["warnings"]


def test_a_correction_may_supersede_a_claim_of_an_earlier_region_by_its_exact_id():
    claims, report = validate_claims(
        [_claim("c1", "correction", "Freeze starts 2026-12-18", ["m10"], supersedes="r1.c3")],
        REGION2, prior_ids=["r1.c3", "r1.c4"], prefix="r2")
    assert claims[0]["id"] == "r2.c1" and claims[0]["supersedes"] == "r1.c3" and not report["warnings"]


def test_unknown_self_or_forward_supersedes_are_dropped_with_a_warning_and_the_claim_kept():
    claims, report = validate_claims([
        _claim("c1", "correction", "Freeze starts 2026-12-18", ["m10"], supersedes="r1.c99"),
        _claim("c2", "fact", "Priya owns the rollback plan", ["m11"], supersedes="c2"),
        _claim("c3", "fact", "Freeze is 2026-12-18", ["m10"], supersedes="c4"),
        _claim("c4", "fact", "Freeze updated to 2026-12-18", ["m11"]),
    ], REGION2, prior_ids=["r1.c1"], prefix="r2")
    assert [c["supersedes"] for c in claims] == [None, None, None, None]
    assert len(report["warnings"]) == 3 and len(claims) == 4


def test_without_prior_ids_a_cross_region_supersedes_is_not_trusted():
    claims, report = validate_claims(
        [_claim("c1", "correction", "Freeze starts 2026-12-18", ["m10"], supersedes="r1.c3")], REGION2, prefix="r2")
    assert claims[0]["supersedes"] is None and "r1.c3" in report["warnings"][0]


def test_literals_of_a_cross_region_correction_are_checked_against_its_own_region_only():
    claims, report = validate_claims(
        [_claim("c1", "correction", "Freeze starts 2026-12-15", ["m10"], supersedes="r1.c1")],   # 12-15 is in region 1
        REGION2, prior_ids=["r1.c1"], prefix="r2")
    assert not claims and report["rejected"][0]["reason"] == "literal_unsupported:2026-12-15"


# ── active claims ────────────────────────────────────────────────────────────

def _c(cid, supersedes=None, importance=2):
    return {"id": cid, "supersedes": supersedes, "importance": importance, "type": "fact", "text": cid, "cites": ["m2"]}


def test_a_chain_of_corrections_leaves_only_the_last_claim():
    live = active_claims([_c("r1.c1"), _c("r2.c1", "r1.c1"), _c("r3.c1", "r2.c1"), _c("r3.c2")])
    assert [c["id"] for c in live] == ["r3.c1", "r3.c2"]


def test_active_claims_does_not_mutate_or_reorder_and_handles_an_empty_list():
    claims = [_c("a"), _c("b", "a"), _c("c")]
    assert [c["id"] for c in active_claims(claims)] == ["b", "c"] and len(claims) == 3
    assert active_claims([]) == []


# ── rendering ────────────────────────────────────────────────────────────────

def test_render_drops_claims_superseded_in_a_later_region_and_marks_the_replacement():
    r1 = _result([_c("r1.c1"), _c("r1.c2", importance=3)], REGION1)
    r1.claims[0].update(text="Freeze starts 2026-12-15", type="constraint", cites=["m2"])
    r1.claims[1].update(text="Target is staging-eu-2", type="decision", cites=["m2"])
    r2 = _result([_c("r2.c1", "r1.c1")], REGION2, narrative={"goal": "ship", "next_steps": ""})
    r2.claims[0].update(text="Freeze starts 2026-12-18", type="correction", cites=["m10"])
    block = render_claims_block([r1, r2])
    assert "2026-12-15" not in block and "(replaces r1.c1)" in block
    assert "- r2.c1 [correction] Freeze starts 2026-12-18 [cites: m10] (replaces r1.c1)" in block
    assert "- r1.c2 [decision] Target is staging-eu-2 [cites: m2]" in block
    assert "region m2..m3 (2 messages):" in block and "region m10..m11 (2 messages):" in block
    assert "narrative: goal: ship" in block and "next_steps" not in block


def test_only_the_newest_regions_narrative_is_shown_by_default_because_older_ones_go_stale():
    r1 = _result([_c("r1.c1")], REGION1, narrative={"decisions": "freeze 2026-12-15"})
    r1.claims[0].update(text="Freeze starts 2026-12-15", cites=["m2"])
    r2 = _result([_c("r2.c1", "r1.c1")], REGION2, narrative={"decisions": "freeze corrected to 2026-12-18"})
    r2.claims[0].update(text="Freeze starts 2026-12-18", cites=["m10"])
    default = render_claims_block([r1, r2])
    assert "freeze corrected to 2026-12-18" in default and "freeze 2026-12-15" not in default
    assert "freeze 2026-12-15" in render_claims_block([r1, r2], narratives="all")
    assert "narrative:" not in render_claims_block([r1, r2], narratives="none")


def test_the_newest_narrative_is_the_newest_compacted_region_even_if_a_later_one_was_rejected():
    r1 = _result([_c("r1.c1")], REGION1, narrative={"goal": "first"})
    r1.claims[0].update(text="Freeze starts 2026-12-15", cites=["m2"])
    bad = _result([], REGION2, status="shadow_rejected", narrative={"goal": "never shown"})
    block = render_claims_block([r1, bad])
    assert "goal: first" in block and "never shown" not in block


def test_render_filters_by_importance_and_excludes_rejected_regions():
    r1 = _result([_c("r1.c1", importance=1), _c("r1.c2", importance=2), _c("r1.c3", importance=3)], REGION1)
    for c in r1.claims:
        c.update(text=f"text {c['id']}", cites=["m2"])
    bad = _result([], REGION2, status="shadow_rejected")
    assert "r1.c1" in render_claims_block([r1, bad], min_importance=1)
    block = render_claims_block([r1, bad], min_importance=2)
    assert "r1.c1" not in block and "r1.c2" in block and "r1.c3" in block
    assert "m10" not in block                                    # a rejected region contributes nothing
    assert "r1.c2" not in render_claims_block([r1], min_importance=3)


def test_a_replacement_inherits_the_importance_of_what_it_replaces_so_it_is_never_filtered_out():
    r1 = _result([_c("r1.c1", importance=3)], REGION1)
    r1.claims[0].update(text="old value 2026-12-15", cites=["m2"])
    r2 = _result([_c("r2.c1", "r1.c1", importance=1)], REGION2)
    r2.claims[0].update(text="new value 2026-12-18", cites=["m10"])
    block = render_claims_block([r1, r2], min_importance=2)
    assert "old value" not in block and "new value" in block
    assert effective_importance(r1.claims + r2.claims) == {"r1.c1": 3, "r2.c1": 3}
    r3 = _result([_c("r3.c1", "r2.c1", importance=1)], REGION2)
    assert effective_importance(r1.claims + r2.claims + r3.claims)["r3.c1"] == 3            # through the whole chain


def test_a_low_importance_claim_with_nothing_to_replace_is_still_filtered():
    r1 = _result([_c("r1.c1", importance=1)], REGION1)
    r1.claims[0].update(text="minor aside 2026-12-15", cites=["m2"])
    assert "minor aside" not in render_claims_block([r1], min_importance=2)


# ── compact_region with prior claims ─────────────────────────────────────────

ANSWER2 = json.dumps({
    "claims": [_claim("c1", "correction", "Freeze starts 2026-12-18", ["m10"], supersedes="r1.c1", importance=3),
               _claim("c2", "commitment", "Priya owns the rollback plan", ["m11"], importance=3)],
    "narrative": {"goal": "ship"},
})
PRIOR = [{"id": "r1.c1", "type": "constraint", "text": "Freeze starts 2026-12-15"}]


async def test_compact_region_uses_prior_claims_namespaces_ids_and_records_the_version():
    llm = _llm(ANSWER2)
    res = await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r2", prompt_version="claims-v2")
    assert res.status == "shadow_ok" and [c["id"] for c in res.claims] == ["r2.c1", "r2.c2"]
    assert res.claims[0]["supersedes"] == "r1.c1" and res.claims[0]["importance"] == 3
    assert "- r1.c1 [constraint] Freeze starts 2026-12-15" in llm.calls[0][1]["content"]
    assert (res.region_first_id, res.region_last_id, res.region_count, res.prompt_version) == ("m10", "m11", 2, "claims-v2")
    stored = store.get_compaction(res.compaction_id)
    assert stored["prompt_version"] == "claims-v2" and stored["status"] == "shadow_ok"


async def test_the_compaction_id_depends_on_prior_claims_prefix_and_version_so_stale_results_are_not_reused():
    llm = _llm(ANSWER2)
    base = await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r2")
    again = await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r2")
    assert again.cached and again.compaction_id == base.compaction_id and len(llm.calls) == 1
    other_prior = await compact_region("c1", "dad", REGION2, llm, prior_claims=[{**PRIOR[0], "text": "Freeze starts later"}], claim_prefix="r2")
    other_prefix = await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r7")
    other_version = await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r2", prompt_version="claims-v1")
    assert len({base.compaction_id, other_prior.compaction_id, other_prefix.compaction_id, other_version.compaction_id}) == 4
    assert len(llm.calls) == 4


async def test_force_recomputes_a_cached_region():
    llm = _llm(ANSWER2)
    await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r2")
    fresh = await compact_region("c1", "dad", REGION2, llm, prior_claims=PRIOR, claim_prefix="r2", force=True)
    assert not fresh.cached and len(llm.calls) == 2


async def test_a_v1_prompt_version_yields_claims_with_the_default_importance():
    answer = json.dumps({"claims": [_claim("c1", "decision", "Target is staging-eu-2", ["m2"])], "narrative": {}})
    res = await compact_region("c1", "dad", REGION1, _llm(answer), prompt_version="claims-v1")
    assert res.claims[0]["importance"] == 2 and res.claims[0]["id"] == "c1" and res.prompt_version == "claims-v1"


async def test_an_unknown_prompt_version_falls_back_to_the_default():
    answer = json.dumps({"claims": [_claim("c1", "decision", "Target is staging-eu-2", ["m2"])], "narrative": {}})
    res = await compact_region("c1", "dad", REGION1, _llm(answer), prompt_version="claims-v7")
    assert res.prompt_version == "claims-v2"


async def test_two_regions_compacted_in_sequence_end_with_the_corrected_value_only():
    first = await compact_region("c1", "dad", REGION1, _llm(json.dumps({
        "claims": [_claim("c1", "constraint", "Freeze starts 2026-12-15", ["m2"], importance=3)], "narrative": {}})),
        claim_prefix="r1")
    prior = active_claims(first.claims)
    second = await compact_region("c1", "dad", REGION2, _llm(ANSWER2), prior_claims=prior, claim_prefix="r2")
    block = render_claims_block([first, second])
    assert "2026-12-15" not in block and "2026-12-18" in block and "Priya owns the rollback plan" in block
    assert first.claims[0]["id"] == "r1.c1" and second.claims[0]["supersedes"] == "r1.c1"
