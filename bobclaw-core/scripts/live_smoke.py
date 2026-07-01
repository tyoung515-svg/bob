"""Live three-backend smoke for handoff_009 observation data.

Runs 5 producer/critic combos through real APIs and captures token usage,
latency, verdict distribution. Writes findings to
worker/handoff_011_live_smoke_findings.md.

Combos:
  1. kimi_code        producer + claude_api          critic
  2. kimi_code        producer + deepseek_v4_flash   critic
  3. claude_api       producer + deepseek_v4_flash   critic
  4. deepseek_v4_flash producer + claude_api         critic
  5. deepseek_v4_flash producer + deepseek_v4_flash  critic  (same-family baseline)

Hard spend cap (default $5.00). Aborts before next combo if exceeded.

Usage:
    cd bobclaw-core
    python scripts/live_smoke.py                 # run all 5 combos
    python scripts/live_smoke.py --cap 2.0       # tighter cap
    python scripts/live_smoke.py --combos 1,2    # only specific combos
    python scripts/live_smoke.py --dry-run       # check keys + plan; don't call APIs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Make `core` importable regardless of cwd.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from core.config import config  # noqa: E402
from core.backends.claude import ClaudeClient  # noqa: E402
from core.backends.deepseek import DeepSeekClient  # noqa: E402
from core.backends.kimi import KimiClient  # noqa: E402
from core.nodes.critic import (  # noqa: E402
    CRITIC_DEFAULT_PROMPT_TEMPLATE,
    parse_verdict,
)


# Public pricing (USD per 1M tokens). Approximate; verify against current
# vendor pricing pages before relying on the cost column.
#   - claude-sonnet-5   : platform.claude.com/docs/en/about-claude/pricing
#   - kimi-k2.6 PAYG    : api.moonshot.ai (rates from core/backends/_cost.py)
#   - deepseek-v4-flash : api-docs.deepseek.com/quick_start/pricing
PRICING: dict[str, dict[str, float]] = {
    "claude_api":         {"input": 3.00, "output": 15.00, "note": "claude-sonnet-5"},
    "kimi_code":          {"input": 0.95, "output": 4.00,  "note": "kimi-k2.7-code membership via moonshot.ai"},
    "deepseek_v4_flash":  {"input": 0.07, "output": 0.28,  "note": "deepseek-v4-flash"},
}


PROMPTS: dict[str, str] = {
    "easy": (
        "Define Python's len() builtin in one sentence."
    ),
    "medium": (
        "Explain how Python's GIL affects multi-threaded CPU-bound code "
        "in 1-2 sentences."
    ),
    "subtle": (
        "Briefly explain the difference between `is` and `==` in Python "
        "in 1-2 sentences."
    ),
    "trick": (
        # The premise is false: 0.1 + 0.2 == 0.30000000000000004, NOT 0.3.
        # A correct producer should flag the false premise; a fabricator agrees.
        # The critic gets to surface the disagreement.
        "Briefly explain why 0.1 + 0.2 == 0.3 evaluates to True in Python "
        "in 1-2 sentences."
    ),
    "open": (
        "Briefly summarize when to use a Python coroutine vs a thread "
        "in 1-2 sentences."
    ),
}

# Backwards-compat — single-prompt mode used the original prompt.
DEFAULT_SINGLE_PROMPT = (
    "Briefly explain Python's asyncio.gather in 1-2 sentences. "
    "Be concise but accurate."
)


# -- Backend-agnostic call wrapper ----------------------------------------─

def _make_client(backend: str):
    if backend == "claude_api":
        return ClaudeClient()
    if backend == "kimi_code":
        return KimiClient()
    if backend == "deepseek_v4_flash":
        return DeepSeekClient()
    raise ValueError(f"unknown backend: {backend}")


async def _call(backend: str, system_msg: str, user_msg: str) -> dict[str, Any]:
    """Call a backend and return {content, usage, latency_ms, raw}.

    Handles the Anthropic-vs-OpenAI message-shape divergence:
      - Claude takes `system` as a separate kwarg.
      - Kimi/DeepSeek take a system role in `messages`.
    """
    client = _make_client(backend)
    start = time.monotonic()

    if backend == "claude_api":
        messages = [{"role": "user", "content": user_msg}]
        raw = await client.chat(messages, system=system_msg)
        content = raw["content"][0]["text"]
        u = raw.get("usage", {})
        usage = {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
        }
    else:
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        raw = await client.chat(messages)
        content = raw["choices"][0]["message"]["content"]
        u = raw.get("usage", {})
        usage = {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
        }

    latency_ms = int((time.monotonic() - start) * 1000)
    return {
        "content": content,
        "usage": usage,
        "latency_ms": latency_ms,
    }


def _cost(backend: str, usage: dict[str, int]) -> float:
    p = PRICING[backend]
    return (
        usage["input_tokens"] / 1_000_000 * p["input"]
        + usage["output_tokens"] / 1_000_000 * p["output"]
    )


# -- Combo runner ----------------------------------------------------------

async def run_combo(
    idx: int,
    producer: str,
    critic: str,
    prompt_id: str,
    prompt_text: str,
) -> dict[str, Any]:
    print(f"\n-- Combo {idx} [{prompt_id}]: producer={producer}, critic={critic} --")

    p = await _call(producer, "You are a build worker.", prompt_text)
    p_cost = _cost(producer, p["usage"])
    print(
        f"  producer  : {p['latency_ms']}ms  "
        f"in={p['usage']['input_tokens']} out={p['usage']['output_tokens']}  "
        f"cost=${p_cost:.5f}"
    )
    print(f"  output    : {p['content'][:120]}...")

    critic_user = CRITIC_DEFAULT_PROMPT_TEMPLATE.format(
        subtask_text=prompt_text,
        worker_output=p["content"],
    )
    c = await _call(critic, "You are a critic evaluating worker output.", critic_user)
    c_cost = _cost(critic, c["usage"])
    verdict, reasons = parse_verdict(c["content"])
    print(
        f"  critic    : {c['latency_ms']}ms  "
        f"in={c['usage']['input_tokens']} out={c['usage']['output_tokens']}  "
        f"cost=${c_cost:.5f}"
    )
    print(f"  verdict   : {verdict}  reasons={reasons}")

    return {
        "idx": idx,
        "prompt_id": prompt_id,
        "producer": producer,
        "critic": critic,
        "producer_latency_ms": p["latency_ms"],
        "critic_latency_ms": c["latency_ms"],
        "producer_usage": p["usage"],
        "critic_usage": c["usage"],
        "producer_cost_usd": round(p_cost, 5),
        "critic_cost_usd": round(c_cost, 5),
        "combo_cost_usd": round(p_cost + c_cost, 5),
        "verdict": verdict,
        "reasons": reasons,
        "producer_output_excerpt": p["content"][:200],
    }


# -- Findings doc writer --------------------------------------------------─

def _write_findings(results: list[dict[str, Any]], total_cost: float, cap: float) -> Path:
    multi_prompt = len({r.get("prompt_id", "default") for r in results}) > 1
    suffix = "_extended" if multi_prompt else ""
    # Findings live at <repo-root>/worker/, NOT BoBClaw/worker/.
    out = _REPO.parent.parent / "worker" / f"handoff_011_live_smoke_findings{suffix}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        f"# Live smoke findings (dispatch_011{', extended' if multi_prompt else ''})",
        "",
        f"**Run timestamp:** {datetime.now(timezone.utc).isoformat()}",
        f"**Spend cap:** ${cap:.2f}  **observed total:** ${total_cost:.5f}",
        f"**Turns run:** {len(results)}",
        "",
        "## 1. Per-turn data",
        "",
        "| # | Prompt | Producer | Critic | P-lat (ms) | C-lat (ms) | Tokens (P in/out) | Tokens (C in/out) | Cost USD | Verdict | Reasons |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        reasons_count = len(r.get("reasons", []))
        lines.append(
            f"| {r['idx']} | `{r.get('prompt_id', 'default')}` | "
            f"`{r['producer']}` | `{r['critic']}` | "
            f"{r['producer_latency_ms']} | {r['critic_latency_ms']} | "
            f"{r['producer_usage']['input_tokens']}/{r['producer_usage']['output_tokens']} | "
            f"{r['critic_usage']['input_tokens']}/{r['critic_usage']['output_tokens']} | "
            f"${r['combo_cost_usd']:.5f} | {r['verdict']} | {reasons_count} |"
        )

    lines += [
        "",
        "## 2. Verdict distribution",
        "",
    ]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    for k, v in sorted(counts.items()):
        lines.append(f"- `{k}`: {v}")

    if multi_prompt:
        lines += [
            "",
            "## 2b. Verdict cross-tab (combos x prompts)",
            "",
        ]
        prompts_seen = sorted({r["prompt_id"] for r in results})
        combos_seen = sorted({r["idx"] for r in results})
        header = "| combo | producer -> critic | " + " | ".join(prompts_seen) + " |"
        sep = "|---|---|" + "|".join(["---"] * len(prompts_seen)) + "|"
        lines += [header, sep]
        for combo_idx in combos_seen:
            row_meta = next(r for r in results if r["idx"] == combo_idx)
            cells: list[str] = []
            for pid in prompts_seen:
                match = next(
                    (r for r in results
                     if r["idx"] == combo_idx and r.get("prompt_id") == pid),
                    None,
                )
                cells.append(match["verdict"] if match else "-")
            lines.append(
                f"| {combo_idx} | `{row_meta['producer']}` -> "
                f"`{row_meta['critic']}` | " + " | ".join(cells) + " |"
            )

        lines += [
            "",
            "## 2c. Empty-reasons-list signal (same-family hypothesis)",
            "",
            "Cross-family critics gave specific reasons; same-family critic (combo 5: "
            "`deepseek_v4_flash` + `deepseek_v4_flash`) returned an empty reasons list "
            "in the original 5-combo run. Below: per-turn reasons-count, with combo 5 "
            "highlighted.",
            "",
            "| combo | producer -> critic | prompt | verdict | reasons count |",
            "|---|---|---|---|---|",
        ]
        for r in results:
            cells = (
                f"{r['idx']}" + (" (same-family)" if r["producer"] == r["critic"] else ""),
                f"`{r['producer']}` -> `{r['critic']}`",
                r.get("prompt_id", "default"),
                r["verdict"],
                str(len(r.get("reasons", []))),
            )
            lines.append("| " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 3. Producer outputs (excerpts)",
        "",
    ]
    for r in results:
        header = (
            f"### Turn {r['idx']} [{r.get('prompt_id', 'default')}] "
            f"({r['producer']} -> {r['critic']})"
        )
        lines += [
            header,
            "",
            f"> {r['producer_output_excerpt']}...",
            "",
        ]
        if r.get("reasons"):
            lines += ["**Critic reasons:**", ""]
            for reason in r["reasons"]:
                lines.append(f"- {reason}")
            lines.append("")

    lines += [
        "## 4. Pricing notes",
        "",
        "Cost column uses these per-1M-token rates (verify before quoting elsewhere):",
        "",
    ]
    for backend, p in sorted(PRICING.items()):
        lines.append(
            f"- `{backend}`: input ${p['input']}/M, output ${p['output']}/M  "
            f"({p['note']})"
        )

    lines += [
        "",
        "## 5. Recommendations for handoff_009",
        "",
        "Use the per-combo cost + latency data above as the empirical baseline for "
        "rotation cycle-length tuning. The verdict distribution is a starting "
        "signal for the divergence threshold (LKS v3.1 rule 18) but a single "
        "5-combo run is too small to lock the threshold — recommend at least "
        "20 turns of accumulated `bobclaw.core.fanout` log data before "
        "committing rotation defaults.",
        "",
        "Open questions surfaced in `handoff_009_observation_findings.md` §5 "
        "(rotation cycle length, divergence threshold, state location, target "
        "pool config) remain open — this run informs them but does not resolve "
        "them.",
        "",
    ]

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# -- Main ------------------------------------------------------------------

COMBOS = [
    (1, "kimi_code",         "claude_api"),
    (2, "kimi_code",         "deepseek_v4_flash"),
    (3, "claude_api",        "deepseek_v4_flash"),
    (4, "deepseek_v4_flash", "claude_api"),
    (5, "deepseek_v4_flash", "deepseek_v4_flash"),
]


def _check_keys(combos: list[tuple[int, str, str]]) -> Optional[str]:
    needed: set[str] = set()
    for _, p, c in combos:
        needed.add(p)
        needed.add(c)
    missing: list[str] = []
    if "claude_api" in needed and not config.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if "kimi_code" in needed and not config.KIMI_API_KEY:
        missing.append("KIMI_API_KEY")
    if "deepseek_v4_flash" in needed and not config.DEEPSEEK_API_KEY:
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        return f"missing env keys: {', '.join(missing)}"
    return None


async def main_async(args: argparse.Namespace) -> int:
    selected = COMBOS
    if args.combos:
        wanted = {int(x) for x in args.combos.split(",")}
        selected = [c for c in COMBOS if c[0] in wanted]

    err = _check_keys(selected)
    if err:
        print(f"ABORT: {err}", file=sys.stderr)
        return 2

    if args.prompts:
        wanted_p = [x.strip() for x in args.prompts.split(",") if x.strip()]
        unknown = [p for p in wanted_p if p not in PROMPTS and p != "default"]
        if unknown:
            print(f"ABORT: unknown prompt id(s): {unknown}. "
                  f"Known: {list(PROMPTS)} or 'default'", file=sys.stderr)
            return 2
        prompt_pairs = [
            ("default", DEFAULT_SINGLE_PROMPT) if p == "default"
            else (p, PROMPTS[p])
            for p in wanted_p
        ]
    else:
        prompt_pairs = [("default", DEFAULT_SINGLE_PROMPT)]

    total_turns = len(selected) * len(prompt_pairs)
    print(f"Plan: {len(selected)} combo(s), spend cap ${args.cap:.2f}")
    for idx, p, c in selected:
        print(f"  combo {idx}: {p} -> {c}")
    print(f"  prompts ({len(prompt_pairs)}): {[p[0] for p in prompt_pairs]}")
    print(f"  total turns: {total_turns}  (combos x prompts)")

    if args.dry_run:
        print("\n--dry-run: keys present, plan looks ok. No API calls made.")
        return 0

    results: list[dict[str, Any]] = []
    total = 0.0
    aborted = False
    for prompt_id, prompt_text in prompt_pairs:
        if aborted:
            break
        for idx, producer, critic in selected:
            try:
                r = await run_combo(idx, producer, critic, prompt_id, prompt_text)
            except Exception as exc:
                print(f"  COMBO FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                results.append({
                    "idx": idx,
                    "prompt_id": prompt_id,
                    "producer": producer,
                    "critic": critic,
                    "error": f"{type(exc).__name__}: {exc}",
                    "verdict": "error",
                    "reasons": [str(exc)],
                    "combo_cost_usd": 0.0,
                    "producer_latency_ms": 0,
                    "critic_latency_ms": 0,
                    "producer_usage": {"input_tokens": 0, "output_tokens": 0},
                    "critic_usage": {"input_tokens": 0, "output_tokens": 0},
                    "producer_cost_usd": 0.0,
                    "critic_cost_usd": 0.0,
                    "producer_output_excerpt": "(error)",
                })
                continue
            results.append(r)
            total += r["combo_cost_usd"]
            if total > args.cap:
                print(
                    f"\nABORT: spend cap exceeded "
                    f"(${total:.5f} > ${args.cap:.2f}). "
                    f"Stopping.",
                    file=sys.stderr,
                )
                aborted = True
                break

    findings = _write_findings(results, total, args.cap)
    print(f"\n-- Summary --")
    print(f"turns run: {len(results)} of {total_turns} planned")
    print(f"total observed cost: ${total:.5f}")
    print(f"findings written: {findings}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cap", type=float, default=5.00,
                    help="Hard spend cap in USD (default: 5.00)")
    ap.add_argument("--combos", type=str, default="",
                    help="Comma-separated combo indices to run (default: all)")
    ap.add_argument("--prompts", type=str, default="",
                    help=f"Comma-separated prompt ids: {list(PROMPTS)} or 'default' "
                         f"(default: 'default' = single asyncio.gather prompt)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Check keys + plan, do not call APIs")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
