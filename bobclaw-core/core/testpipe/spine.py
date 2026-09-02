"""Test-pipe — build spine harness + the NEW in-loop LLM audit (SPEC §2, §4, §9 units 4, 5).

The build spine (SPEC §2):
    front → worker(slot) → audit(slot, NEW) → verify-sandbox(VISIBLE tests only)
          → repair* → extract impl

It REUSES the build pipeline's proven, pure building blocks — ``contracts.extract_func``
/ ``contracts.is_safe_impl`` (the security gate), ``contracts.ALLOWED_IMPORTS``, and
``sandbox.run_pytest`` (the isolated verify) — driving them for a SINGLE benchmark item
scored on its VISIBLE tests (the graph's build nodes are contract-first + multi-unit +
emit L0/messages, which does not fit single-item benchmark scoring; the reused pieces are
the deterministic verify/repair core).

**NEW wire (SPEC §4):** after the worker impl an ``audit`` model reviews the impl against
the VISIBLE tests + code (never hidden), and its findings + the pytest ``report.raw`` are
threaded into the repair prompt (richer than today's name-only signal). Guarded by
``config.audit``: with ``audit=off`` the audit stage is SKIPPED and the repair prompt is
byte-for-byte :func:`base_repair_prompt` — i.e. the pre-audit loop, no regression (SPEC §4).
"""
from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from core.build import sandbox
from core.build.contracts import ALLOWED_IMPORTS, extract_func, is_safe_impl
from core.testpipe.fence import PromptRecorder, make_fenced_send
from core.testpipe.front import apply_front, front_context
from core.testpipe.metering import CostMeter, make_metered_send
from core.testpipe.types import AUDIT_OFF, ItemResult, SlotConfig, TestItem

SendFn = Callable[..., Awaitable[str]]
VerifyFn = Callable[..., "tuple[bool, dict]"]

_HEADER = "from __future__ import annotations\nimport " + ", ".join(ALLOWED_IMPORTS) + "\n\n\n"


# ── deterministic codegen + the VISIBLE verify gate ─────────────────────────

def write_item_app(workspace: Path, item: TestItem, impl: Optional[str]) -> None:
    """Write ``solution.py`` (impl or stub) + ``tests/test_visible.py`` (VISIBLE tests
    ONLY — SPEC §7.1). One pytest per visible assertion so a green run's ``passed``
    count equals the visible-test count."""
    ws = Path(workspace)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    sig = item.signature or f"{item.entry_point}()"
    body = impl if impl else f"def {sig}:\n    raise NotImplementedError\n"
    (ws / "solution.py").write_text(_HEADER + body + "\n", encoding="utf-8")
    lines = ["from solution import *  # noqa\n\n"]
    if item.visible_tests:
        for i, t in enumerate(item.visible_tests):
            lines.append(f"def test_visible_{i}():\n    {t}\n\n")
    else:
        lines.append(f"def test_exists():\n    assert callable({item.entry_point})\n")
    (ws / "tests" / "test_visible.py").write_text("".join(lines), encoding="utf-8")


def run_visible_verify(workspace: Path, *, mode: str = "subprocess") -> "tuple[bool, dict]":
    """Run the VISIBLE test suite in the sandbox (SPEC §2). Returns ``(ok, report)``;
    ``ok`` iff at least one test passed and none failed/errored. ``mode='subprocess'``
    keeps the scaffold Docker-free + deterministic (Docker isolation is the live
    sweep's hardening, SPEC §11)."""
    report = sandbox.run_pytest(Path(workspace), mode=mode)
    ok = report.get("passed", 0) > 0 and not report.get("failed") and not report.get("errors")
    return ok, report


# ── prompts (fence-safe: VISIBLE material only) ─────────────────────────────

def impl_prompt(item: TestItem, plan_ctx: str = "") -> str:
    imports = ", ".join(ALLOWED_IMPORTS)
    tests = "\n".join(item.visible_tests) or "(satisfy the docstring)"
    return (
        f"{plan_ctx}Implement this Python function using ONLY the standard library "
        f"(already imported, do NOT add imports: {imports}).\n\n"
        f"{item.prompt}\n\nIt MUST satisfy these visible examples:\n{tests}\n\n"
        f"Return ONLY the complete function definition (def ...). No prose, no fences."
    )


def base_repair_prompt(item: TestItem, impl: str, failing: list[str]) -> str:
    """Today's DETERMINISTIC repair signal (SPEC §4: 'name-only', no LLM findings).

    ``audit=off`` uses this verbatim, so the audit-off loop is byte-identical to the
    pre-audit loop."""
    imports = ", ".join(ALLOWED_IMPORTS)
    fails = ", ".join(failing) or "(some visible checks)"
    tests = "\n".join(item.visible_tests)
    return (
        f"Your implementation of `{item.entry_point}` failed visible checks: {fails}.\n"
        f"Available imports (do not add more): {imports}.\n"
        f"It MUST satisfy:\n{tests}\n\nYour current code:\n{impl}\n\n"
        f"Return ONLY the corrected function definition (def ...). No prose, no fences."
    )


def repair_prompt(
    item: TestItem, impl: str, failing: list[str],
    *, findings: Optional[str] = None, report_raw: Optional[str] = None,
) -> str:
    """Compose the repair prompt. NO findings + NO report ⇒ :func:`base_repair_prompt`
    byte-for-byte (audit=off path). Audit-on appends the richer signal as a pure tail
    (SPEC §4)."""
    base = base_repair_prompt(item, impl, failing)
    if not findings and not report_raw:
        return base
    tail = ""
    if findings:
        tail += "\n\nAUDIT FINDINGS (address these):\n" + findings
    if report_raw:
        tail += "\n\nPYTEST OUTPUT:\n" + report_raw
    return base + tail


def audit_prompt(item: TestItem, impl: str, report_raw: str) -> str:
    """The NEW in-loop audit prompt (SPEC §4): review impl vs VISIBLE tests + code.

    Never sees hidden tests; ``report_raw`` is the VISIBLE test run's output only."""
    tests = "\n".join(item.visible_tests)
    return (
        "You are reviewing a candidate implementation against its VISIBLE tests and the "
        "code. Identify likely bugs or spec mismatches. Do NOT rewrite the function; list "
        f"concise findings.\n\n{item.prompt}\n\nVisible tests:\n{tests}\n\n"
        f"Candidate code:\n{impl}\n\nTest output:\n{report_raw}\n\nFindings:"
    )


# ── the build spine ─────────────────────────────────────────────────────────

async def run_build_spine(
    item: TestItem,
    config: SlotConfig,
    *,
    make_send: Callable[[str], SendFn],
    workspace: Path,
    verify_fn: VerifyFn = run_visible_verify,
    sandbox_mode: str = "subprocess",
) -> ItemResult:
    """Run front → worker → (audit) → verify → repair* for ONE item (SPEC §2, §4).

    ``make_send(stage)`` yields a fence+metered send tagged by stage (front/worker/
    audit/repair). ``verify_fn`` is injectable so unit tests avoid the subprocess;
    the E2E uses the real :func:`run_visible_verify`. Returns an :class:`ItemResult`
    (``hidden_pass`` is left False — the external scorer fills it, SPEC §7.2)."""
    result = ItemResult(item_id=item.id, config_name=config.name,
                        provenance=config.provenance())

    # ── front (SPEC §3) ──────────────────────────────────────────────────────
    plan = await apply_front(item, config, send=make_send("front"))
    plan_ctx = front_context(plan)

    # ── worker (SPEC §3) ─────────────────────────────────────────────────────
    worker_send = make_send("worker")
    try:
        raw = await worker_send([{"role": "user", "content": impl_prompt(item, plan_ctx)}],
                                config.worker)
    except Exception as exc:  # noqa: BLE001 — a dead worker surfaces, never crashes the item
        result.error = f"worker_failed: {exc}"
        write_item_app(workspace, item, None)
        return result
    impl = extract_func(str(raw), item.entry_point)
    if impl and not is_safe_impl(impl)[0]:
        impl = None  # unsafe impl dropped (keeps the stub → fails visibly, never run)
    write_item_app(workspace, item, impl)
    result.impl = impl

    ok, report = verify_fn(workspace, mode=sandbox_mode)
    result.visible_pass = ok

    # ── repair loop with the NEW in-loop audit (SPEC §4) ─────────────────────
    audit_on = config.audit != AUDIT_OFF
    findings_seen: list[str] = []
    round_i = 0
    while not ok and round_i < config.repair and impl is not None:
        report_raw = report.get("raw", "") if isinstance(report, dict) else ""
        failing = list(report.get("failing", [])) if isinstance(report, dict) else []

        findings: Optional[str] = None
        if audit_on:
            # NEW audit stage: review impl vs VISIBLE tests + code, thread findings.
            try:
                findings = (await make_send("audit")(
                    [{"role": "user", "content": audit_prompt(item, impl, report_raw)}],
                    config.audit,
                ) or "").strip() or None
            except Exception:  # noqa: BLE001 — an audit hiccup degrades to name-only repair
                findings = None
            if findings:
                findings_seen.append(findings)

        rp = repair_prompt(item, impl, failing,
                          findings=findings,
                          report_raw=report_raw if audit_on else None)
        try:
            raw = await make_send("repair")([{"role": "user", "content": rp}], config.worker)
        except Exception:  # noqa: BLE001
            break
        fixed = extract_func(str(raw), item.entry_point)
        if fixed and not is_safe_impl(fixed)[0]:
            fixed = None
        if fixed:
            impl = fixed
            write_item_app(workspace, item, impl)
            ok, report = verify_fn(workspace, mode=sandbox_mode)
        round_i += 1

    result.impl = impl
    result.visible_pass = ok
    result.rounds = round_i
    result.audit_findings = tuple(findings_seen)
    return result


def build_send_factory(
    raw_send: SendFn, item: TestItem, recorder: PromptRecorder, meter: CostMeter,
) -> Callable[[str], SendFn]:
    """A per-stage fence+meter send factory: ``make(stage) → fenced(metered(raw))``.

    Fence wraps meter (a leak raises BEFORE any spend is counted) wraps the raw seam.
    Shared ``recorder`` + ``meter`` accumulate across every stage of the item."""
    def make(stage: str) -> SendFn:
        return make_fenced_send(
            make_metered_send(raw_send, meter), item, recorder=recorder, stage=stage
        )
    return make
