"""Live claims-v2: the compaction chain, and claims standing in for omitted history inside the token budget."""
from __future__ import annotations

import json
import re

import pytest

from core.context import assembler, compactor, store
from core.context.history import build_prior_conversation

BACKEND = "deepseek_v4_flash"
WINDOW = 20_000                    # -> history budget 10,000 tokens, soft trigger 5,000, protected tail 8,000


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "ctx.db"))
    monkeypatch.setenv("BOB_CTX_WINDOW_DEEPSEEK_V4_FLASH", str(WINDOW))
    for key in ("BOB_CONTEXT_FIT_ENABLED", "BOB_CLAIMS_IN_PROMPT", "BOB_COMPACTOR_ENABLED", "BOB_COMPACTOR_SHADOW",
                "BOB_CTX_PROFILE", "BOB_CTX_HISTORY_FRACTION", "BOB_COMPACTOR_REGION_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    compactor._LOCKS.clear()
    compactor._COOLDOWN.clear()
    compactor._TASKS.clear()


def _history(n=100, pad=1_100):
    out = []
    for i in range(n):
        if i % 2 == 0:
            text = f"Fact {i}: the setting number {i} is {5000 + i}. " + "x" * pad
        else:
            text = f"Noted setting {i - 1}. " + "y" * pad
        out.append({"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": text,
                    "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00"})
    return out


def _llm(calls=None):
    """Offline stand-in for the compactor seat: one verbatim claim per early user message of the region."""
    calls = calls if calls is not None else []

    async def call(messages):
        calls.append(messages)
        found = re.findall(r"^### (\S+) \| user \|[^\n]*\n(.*?)(?=^### |\Z)", messages[-1]["content"], re.M | re.S)
        claims = [{"id": f"c{n}", "type": "fact", "importance": 3, "text": body.strip().split(".")[0],
                   "cites": [mid], "supersedes": None} for n, (mid, body) in enumerate(found[:3], start=1)]
        return json.dumps({"claims": claims, "narrative": {"goal": "settings"}})

    call.backend = "fake"
    call.calls = calls
    return call


async def _compacted(history, user="dad", conv="c1", runs=3, llm=None):
    store.record_history(conv, user, history)
    llm = llm or _llm()
    for _ in range(runs):
        await compactor.run_compaction(conv, user, WINDOW, llm)
    return llm


def _build(history, user="dad", conv="c1", **kw):
    return build_prior_conversation(history, backend_override=BACKEND, conversation_id=conv, user_id=user, **kw)


def _cited(text):
    return {i for line in text.splitlines() if line.startswith("- ") for i in re.findall(r"\[cites: ([^\]]+)\]", line)
            for i in i.split(", ")}


# ── the chain ────────────────────────────────────────────────────────────────

async def test_regions_are_compacted_in_order_each_seeing_the_claims_before_it():
    llm = await _compacted(_history(), runs=1)
    assert len(llm.calls) >= 2                                                # more than one 12K-token region
    first, second = llm.calls[0][1]["content"], llm.calls[1][1]["content"]
    assert "ALREADY-KNOWN" not in first and "ALREADY-KNOWN CLAIMS" in second
    assert "r1.c1" in second                                                  # region 2 can supersede region 1 by id
    recs = store.list_compactions("c1", "dad")
    assert [r["status"] for r in recs] == ["shadow_ok"] * len(recs)
    rows = [(m["message_id"], m["content_sha256"]) for m in store.list_messages("c1", "dad")]
    chain = compactor.chain_of(rows, recs)
    assert chain[0]["first"] == 2 and all(b["first"] == a["last"] + 1 for a, b in zip(chain, chain[1:]))


async def test_a_finished_chain_costs_no_further_model_calls():
    llm = await _compacted(_history(), runs=3)
    calls = len(llm.calls)
    assert await compactor.run_compaction("c1", "dad", WINDOW, llm) == []
    assert len(llm.calls) == calls


async def test_a_run_extends_the_chain_by_at_most_the_per_run_cap_and_resumes_where_it_stopped(monkeypatch):
    monkeypatch.setattr(compactor, "MAX_REGIONS_PER_RUN", 1)
    history = _history()
    store.record_history("c1", "dad", history)
    llm = _llm()
    first = await compactor.run_compaction("c1", "dad", WINDOW, llm)
    second = await compactor.run_compaction("c1", "dad", WINDOW, llm)
    assert len(first) == 1 and len(second) == 1
    assert first[0].region_last_id != second[0].region_last_id
    assert "ALREADY-KNOWN" in llm.calls[1][1]["content"]


async def test_a_rejected_region_halts_the_chain_and_is_not_retried_every_turn():
    store.record_history("c1", "dad", _history())

    async def bad(messages):
        return json.dumps({"claims": [{"id": "c1", "type": "fact", "text": "Nope 7777", "cites": ["m99"]}], "narrative": {}})

    bad.backend = "fake"
    first = await compactor.run_compaction("c1", "dad", WINDOW, bad)
    assert [r.status for r in first] == ["shadow_rejected"]
    again = await compactor.run_compaction("c1", "dad", WINDOW, bad)
    assert [r.status for r in again] == ["shadow_rejected"] and again[0].cached          # no new model call


async def test_a_provider_failure_puts_the_conversation_on_cooldown():
    store.record_history("c1", "dad", _history())
    calls = []

    async def boom(messages):
        calls.append(1)
        raise RuntimeError("provider down")

    boom.backend = "fake"
    assert [r.status for r in await compactor.run_compaction("c1", "dad", WINDOW, boom)] == ["failed"]
    assert await compactor.run_compaction("c1", "dad", WINDOW, boom) == [] and len(calls) == 1


async def test_a_conversation_that_starts_with_an_assistant_message_is_not_compacted_into_a_gap():
    history = [{"id": "g0", "role": "assistant", "content": "Welcome " + "z" * 800}, *_history(60)]
    store.record_history("c1", "dad", history)
    llm = _llm()
    assert await compactor.run_compaction("c1", "dad", WINDOW, llm) == [] and llm.calls == []


def test_chain_of_ignores_edited_messages_other_versions_failed_and_gapped_records():
    msgs = _history(30)
    rows = [(m["id"], store.sha256_text(m["content"])) for m in msgs]

    def rec(a, b, **kw):
        base = {"status": "shadow_ok", "prompt_version": "claims-v2", "region_first_id": rows[a][0],
                "region_last_id": rows[b][0], "region_count": b - a + 1, "created_ts": f"t{a:03d}",
                "input_sha256": store.sha256_text("|".join(sha for _m, sha in rows[a:b + 1]))}
        return {**base, **kw}

    ok = [rec(2, 9), rec(10, 17)]
    assert [(c["first"], c["last"]) for c in compactor.chain_of(rows, ok)] == [(2, 9), (10, 17)]
    assert [(c["first"], c["last"]) for c in compactor.chain_of(rows, [rec(2, 9), rec(12, 17)])] == [(2, 9)]      # gap
    assert compactor.chain_of(rows, [rec(4, 9)]) == []                                                           # not at the start
    assert [(c["first"], c["last"]) for c in compactor.chain_of(rows, [rec(2, 9), rec(10, 17, status="failed")])] == [(2, 9)]
    assert compactor.chain_of(rows, [rec(2, 9, prompt_version="claims-v1")]) == []
    assert compactor.chain_of(rows, [rec(2, 9, input_sha256="0" * 64)]) == []                                   # text changed
    assert compactor.chain_of(rows, [rec(2, 9, region_count=7)]) == []
    first_wins = [rec(2, 9, created_ts="t001", compaction_id="old"), rec(2, 9, created_ts="t002", compaction_id="new")]
    assert compactor.chain_of(rows, first_wins)[0]["rec"]["compaction_id"] == "old"


# ── the prompt ───────────────────────────────────────────────────────────────

async def test_claims_stand_in_for_the_omitted_messages_inside_the_budget():
    history = _history()
    await _compacted(history)
    text, meta = _build(history)
    assert "[compacted history:" in text and "Fact 6: the setting number 6 is 5006" in text
    assert "user: Fact 6:" not in text                                         # the raw message is gone, its claim is there
    assert text.startswith("Prior conversation:\nuser: Fact 0:") and "assistant: Noted setting 98." in text   # head + newest
    assert meta["claims"]["regions"] >= 1 and meta["claims"]["claims_shown"] >= 3
    assert meta["tokens_est_after"] <= meta["budget_tokens"] == 10_000


async def test_only_what_the_claims_do_not_cover_is_announced_as_omitted():
    history = _history()
    await _compacted(history)
    text, meta = _build(history)
    covered, uncovered = meta["claims"]["messages_covered"], meta["claims"]["messages_uncovered"]
    assert covered + uncovered == meta["messages_omitted"]
    note = re.search(r"\[context note: (\d+) message", text)
    if uncovered:
        assert note and int(note.group(1)) == uncovered
    else:
        assert not note
    assert text.index("[compacted history:") < (text.index("[context note:") if note else len(text))


async def test_no_cited_message_is_also_shown_raw():
    history = _history()
    await _compacted(history)
    text, _meta = _build(history)
    raw = {int(n) for n in re.findall(r"^user: Fact (\d+):", text, re.M)}
    cited = {int(i[1:]) for i in _cited(text)}
    assert cited and not (cited & raw)


async def test_without_a_chain_the_prompt_is_the_plain_phase_0_result():
    history = _history()
    store.record_history("c1", "dad", history)
    text, meta = _build(history)
    assert "[compacted history:" not in text and "[context note:" in text and "claims" not in meta


async def test_switching_claims_off_restores_the_plain_omission_note(monkeypatch):
    history = _history()
    await _compacted(history)
    assert "[compacted history:" in _build(history)[0]
    monkeypatch.setenv("BOB_CLAIMS_IN_PROMPT", "false")
    plain, meta = _build(history)
    assert "[compacted history:" not in plain and "claims" not in meta
    monkeypatch.delenv("BOB_CLAIMS_IN_PROMPT")
    monkeypatch.setenv("BOB_CONTEXT_FIT_ENABLED", "false")
    flat, none = _build(history)
    assert none is None and flat.startswith("Prior conversation:\nuser: Fact 0:") and "[compacted" not in flat


async def test_a_history_that_fits_never_touches_the_store(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the store must not be read when nothing is omitted")

    monkeypatch.setattr(store, "list_compactions", boom)
    text, meta = _build(_history(6, pad=50))
    assert "[context note:" not in text and meta["messages_omitted"] == 0


async def test_an_edited_message_invalidates_its_region_and_everything_after_it():
    history = _history()
    await _compacted(history)
    before = _build(history)[1]["claims"]["regions"]
    edited = [dict(m) for m in history]
    edited[10]["content"] = "Fact 10: the setting number 10 is 9999. " + "x" * 1_100            # inside region 1
    text, meta = _build(edited)
    assert before >= 1 and "claims" not in meta and "[compacted history:" not in text


async def test_claims_are_scoped_to_the_owner():
    history = _history()
    await _compacted(history, user="dad")
    assert "[compacted history:" in _build(history, user="dad")[0]
    text, meta = _build(history, user="luna")
    assert "[compacted history:" not in text and "claims" not in meta


async def test_a_history_with_an_id_less_message_gets_the_plain_note():
    history = _history()
    await _compacted(history)
    anonymous = [dict(m) for m in history]
    anonymous[50].pop("id")
    assert "[compacted history:" not in _build(anonymous)[0]


async def test_the_claims_block_is_capped_at_a_share_of_the_budget(monkeypatch):
    history = _history()
    await _compacted(history)
    monkeypatch.setattr(assembler, "CLAIMS_MAX_FRACTION", 0.0001)             # 1 token: nothing can fit
    text, meta = _build(history)
    assert "[compacted history:" not in text and "claims" not in meta
    monkeypatch.setattr(assembler, "CLAIMS_MAX_FRACTION", 0.02)               # 200 tokens: fewer regions than the chain has
    capped = _build(history)[1].get("claims")
    monkeypatch.setattr(assembler, "CLAIMS_MAX_FRACTION", 0.15)
    full = _build(history)[1]["claims"]
    assert capped is None or capped["regions"] < full["regions"]


async def test_a_failure_in_the_assembler_falls_back_to_the_plain_note(monkeypatch):
    history = _history()
    await _compacted(history)

    def boom(*a, **k):
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(compactor, "chain_of", boom)
    text, meta = _build(history)
    assert "[context note:" in text and "[compacted history:" not in text and meta["messages_omitted"] > 0


async def test_a_correction_in_a_later_region_replaces_the_older_claim_in_the_prompt():
    history = _history()
    history[44]["content"] = "Correction: the setting number 4 is now 6004, not 5004. " + "x" * 1_100     # a later region than m4
    store.record_history("c1", "dad", history)

    async def llm(messages):
        user = messages[-1]["content"]
        claims = []
        if "### m4 |" in user:
            claims.append({"id": "c1", "type": "fact", "importance": 3, "text": "Setting number 4 is 5004",
                           "cites": ["m4"], "supersedes": None})
        if "### m44 |" in user:
            prior = re.search(r"- (r\d+\.c\d+) \[fact\] Setting number 4 is 5004", user)
            claims.append({"id": "c1", "type": "correction", "importance": 3, "text": "Setting number 4 is now 6004",
                           "cites": ["m44"], "supersedes": prior.group(1) if prior else None})
        return json.dumps({"claims": claims, "narrative": {}})

    llm.backend = "fake"
    for _ in range(3):
        await compactor.run_compaction("c1", "dad", WINDOW, llm)
    text, _meta = _build(history)
    assert "Setting number 4 is now 6004" in text and "Setting number 4 is 5004" not in text
    assert re.search(r"\(replaces r\d+\.c\d+\)", text)


# ── switches and trigger ─────────────────────────────────────────────────────

def test_compaction_is_on_by_default_and_every_kill_switch_works(monkeypatch):
    assert compactor.compactor_enabled() and compactor.claims_in_prompt_enabled()
    for var, off in (("BOB_COMPACTOR_ENABLED", "false"), ("BOB_COMPACTOR_SHADOW", "false"), ("BOB_CONTEXT_FIT_ENABLED", "false")):
        monkeypatch.setenv(var, off)
        assert not compactor.compactor_enabled(), var
        monkeypatch.delenv(var)
    monkeypatch.setenv("BOB_COMPACTOR_SHADOW", "true")                          # the old flag still means "on"
    assert compactor.compactor_enabled()
    monkeypatch.setenv("BOB_CLAIMS_IN_PROMPT", "false")
    assert not compactor.claims_in_prompt_enabled() and compactor.compactor_enabled()
    monkeypatch.delenv("BOB_CLAIMS_IN_PROMPT")
    monkeypatch.setenv("BOB_CONTEXT_FIT_ENABLED", "false")
    assert not compactor.claims_in_prompt_enabled()


async def test_only_conversations_on_a_known_cloud_backend_are_ever_scheduled(monkeypatch):
    seen = []

    async def stub(conv, user, window, llm=None):
        seen.append((conv, user, window))
        return []

    monkeypatch.setattr(compactor, "run_compaction", stub)

    def state(backend, before=900_000, window=1_000_000, conv="c1"):
        return {"conversation_id": conv, "user_id": "dad",
                "history_fit": {"backend": backend, "window_tokens": window, "tokens_est_before": before}}

    for local in ("qwen_research", "local_strata", "local", "ollama", "lmstudio", None, "", "some-new-backend"):
        assert compactor.maybe_schedule_compaction(state(local)) is False, local
    assert compactor.maybe_schedule_compaction(state(BACKEND, before=100_000)) is False         # below the soft trigger
    assert compactor.maybe_schedule_compaction(state(BACKEND, conv="")) is False
    assert compactor.maybe_schedule_compaction({"conversation_id": "c1", "user_id": "dad"}) is False
    assert compactor.maybe_schedule_compaction(state(BACKEND)) is True
    import asyncio
    await asyncio.gather(*list(compactor._TASKS))
    assert seen == [("c1", "dad", 1_000_000)]
    monkeypatch.setenv("BOB_COMPACTOR_ENABLED", "false")
    assert compactor.maybe_schedule_compaction(state(BACKEND)) is False
