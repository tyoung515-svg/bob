"""Test-pipe — build spine + the NEW in-loop LLM audit (SPEC §2, §4, §9 units 4, 5).

The load-bearing acceptance item: **audit=off ⇒ the build loop is byte-identical to the
pre-audit loop** (no audit model call, repair prompt == the deterministic base signal),
and audit=on threads findings + the pytest report into a richer repair prompt.
"""
from __future__ import annotations

from core.build.contracts import extract_func
from core.testpipe.fence import PromptRecorder
from core.testpipe.fixtures import fixture_items
from core.testpipe.metering import CostMeter
from core.testpipe.spine import (
    base_repair_prompt,
    build_send_factory,
    repair_prompt,
    run_build_spine,
)
from core.testpipe.types import AUDIT_OFF, SlotConfig

_WRONG = "def add(a, b):\n    return 0"
_RIGHT = "def add(a, b):\n    return a + b"


def _add_item():
    return fixture_items()[0]  # fx_add


def _make_raw(worker_impl=_WRONG, repair_impl=_RIGHT, audit_text="finding: returns 0 not a+b"):
    async def raw(messages, backend, model=None):
        c = messages[-1]["content"]
        if "You are reviewing" in c:          # audit stage
            return audit_text
        if "failed visible checks" in c:      # repair stage
            return repair_impl
        if "Implement this Python function" in c:  # worker stage
            return worker_impl
        return ""
    return raw


def _make_verify(seq):
    st = {"i": 0}

    def vf(ws, mode="subprocess"):
        r = seq[min(st["i"], len(seq) - 1)]
        st["i"] += 1
        return r
    return vf


def _stages(rec):
    return [p["stage"] for p in rec.prompts]


def _prompt_at(rec, stage):
    return next(p["messages"][-1]["content"] for p in rec.prompts if p["stage"] == stage)


# ── pure byte-identity (SPEC §4) ────────────────────────────────────────────

def test_repair_prompt_audit_off_is_base_byte_identical():
    it = _add_item()
    failing = ["test_visible_0"]
    assert repair_prompt(it, _WRONG, failing) == base_repair_prompt(it, _WRONG, failing)
    # audit-on is a PURE ADDITIVE superset of the base signal
    witha = repair_prompt(it, _WRONG, failing, findings="F", report_raw="R")
    assert witha.startswith(base_repair_prompt(it, _WRONG, failing))
    assert "AUDIT FINDINGS" in witha and "PYTEST OUTPUT" in witha


# ── worker extraction + fail-soft ───────────────────────────────────────────

async def test_worker_extracts_impl_and_passes_round0(tmp_path):
    it = _add_item()
    rec, meter = PromptRecorder(), CostMeter()
    make_send = build_send_factory(_make_raw(worker_impl=_RIGHT), it, rec, meter)
    cfg = SlotConfig(name="B0", worker="deepseek_v4_flash", audit=AUDIT_OFF, repair=0)
    res = await run_build_spine(it, cfg, make_send=make_send, workspace=tmp_path,
                                verify_fn=_make_verify([(True, {"passed": 1})]))
    assert res.visible_pass and res.rounds == 0
    assert "def add" in (res.impl or "")
    assert _stages(rec) == ["worker"]          # no front (passthrough), no audit (off)
    assert res.provenance["worker"] == "hosted" and res.provenance["audit"] == "local"


async def test_unsafe_impl_is_dropped(tmp_path):
    it = _add_item()
    rec, meter = PromptRecorder(), CostMeter()
    raw = _make_raw(worker_impl="def add(a, b):\n    import os\n    return os.getpid()")
    make_send = build_send_factory(raw, it, rec, meter)
    cfg = SlotConfig(name="B0", worker="deepseek_v4_flash", audit=AUDIT_OFF, repair=1)
    res = await run_build_spine(it, cfg, make_send=make_send, workspace=tmp_path,
                                verify_fn=_make_verify([(False, {"failing": [], "raw": ""})]))
    assert res.impl is None and not res.visible_pass    # unsafe dropped → stub kept
    assert res.rounds == 0                               # impl None ⇒ no repair attempt


async def test_worker_exception_surfaces_not_crashes(tmp_path):
    it = _add_item()

    async def boom(messages, backend, model=None):
        raise RuntimeError("backend exploded")

    rec, meter = PromptRecorder(), CostMeter()
    make_send = build_send_factory(boom, it, rec, meter)
    cfg = SlotConfig(name="B0", worker="deepseek_v4_flash", audit=AUDIT_OFF, repair=0)
    res = await run_build_spine(it, cfg, make_send=make_send, workspace=tmp_path,
                                verify_fn=_make_verify([(True, {"passed": 1})]))
    assert res.error and "worker_failed" in res.error and res.impl is None


# ── audit=off byte-identical loop vs audit=on richer loop (SPEC §4) ─────────

async def test_audit_off_loop_has_no_audit_call_and_base_repair(tmp_path):
    it = _add_item()
    rec, meter = PromptRecorder(), CostMeter()
    make_send = build_send_factory(_make_raw(), it, rec, meter)
    cfg = SlotConfig(name="B0", worker="deepseek_v4_flash", audit=AUDIT_OFF, repair=1)
    seq = [(False, {"failing": ["test_visible_0"], "raw": "E   assert add(1, 2) == 3"}),
           (True, {"passed": 1})]
    res = await run_build_spine(it, cfg, make_send=make_send, workspace=tmp_path,
                                verify_fn=_make_verify(seq))
    assert res.visible_pass and res.rounds == 1
    # NO audit stage was ever invoked (byte-identical to the pre-audit loop)
    assert "audit" not in _stages(rec)
    assert _stages(rec) == ["worker", "repair"]
    # the repair prompt is EXACTLY the deterministic base signal (no findings/report)
    expected_impl = extract_func(_WRONG, "add")
    assert _prompt_at(rec, "repair") == base_repair_prompt(it, expected_impl, ["test_visible_0"])
    assert res.audit_findings == ()


async def test_audit_on_threads_findings_into_repair(tmp_path):
    it = _add_item()
    rec, meter = PromptRecorder(), CostMeter()
    make_send = build_send_factory(_make_raw(), it, rec, meter)
    cfg = SlotConfig(name="B0", worker="deepseek_v4_flash", audit="minimax", repair=1)
    seq = [(False, {"failing": ["test_visible_0"], "raw": "E   assert add(1, 2) == 3"}),
           (True, {"passed": 1})]
    res = await run_build_spine(it, cfg, make_send=make_send, workspace=tmp_path,
                                verify_fn=_make_verify(seq))
    assert res.visible_pass and res.rounds == 1
    assert "audit" in _stages(rec)                       # the NEW stage ran
    repair_text = _prompt_at(rec, "repair")
    assert "AUDIT FINDINGS" in repair_text and "returns 0 not a+b" in repair_text
    assert "PYTEST OUTPUT" in repair_text                # report.raw threaded in (§4)
    assert res.audit_findings and res.provenance["audit"] == "hosted"
