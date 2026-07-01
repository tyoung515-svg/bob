"""MS2-R3 — research subagent: IterResearch round reconstruction + condensed-return firewall.

The ONE genuinely-new memory primitive of the research lane. Each round REBUILDS a LEAN workspace from
``{question + evolving report + last tool turn}`` where the **evolving report is read from the LEDGER SLICE**
(``project.read_ledger_at`` / ``session.ledger_slice`` = truth-at-a-commit, durable — NOT in-window history);
the raw retrieved chunk + the round's raw reasoning are DISCARDED each round (ephemera, Tongyi "Heavy Mode").
The subagent calls the R1 retriever each round and returns a STRUCTURED, ``≤RESEARCH_RETURN_TOKEN_CEILING``
artifact ``{claims[], sources[], report_fragment}`` (the §2.5 condensed-return firewall — burn tens of
thousands of tokens internally, return only 1-2k). **Internal burn is bound to the MS-4 per-branch
reservation** — a runaway round trips the in-branch breaker (BIND-02), reading ONLY this branch's own
reservation (no shared/sibling poll).

Additive + composes landed primitives (``read_ledger_at`` / ``ledger_slice`` /
``commit_trajectory_with_provenance`` / ``approx_tokens`` / ``branch_spend_result`` / the R1 retriever /
``entailment.Source``) — it does NOT invent a memory store. Import-light: no network / git / Qdrant at import;
the model, retriever, and report store are injected.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from core.config import (
    RESEARCH_RETURN_TOKEN_CEILING,
    RESEARCH_MAX_ROUNDS,
    RESEARCH_MAX_CLAIMS,
    RESEARCH_MAX_SOURCES,
)
from core.nodes.budget_runtime import approx_tokens, branch_spend_result
from core.ledger.types import OVERSPEND_TRIGGER
from core.verify.entailment import Source, RetrieveRequest
from core.ledger.project import read_ledger_at
from core.ledger.session import ledger_slice, commit_trajectory_with_provenance

logger = logging.getLogger("bobclaw.research.subagent")


# ── Exception ──────────────────────────────────────────────────────────────

class ResearchSubagentError(RuntimeError):
    """Construction/config misuse: empty question, max_rounds<=0, non-positive ceiling/cap, missing model_send."""
    pass


# ── Frozen dataclasses ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class RoundArtifact:
    """The parsed structured output of one research round."""
    round_idx: int
    claims: tuple = ()          # each a dict {subject, predicate, numeric_value, cited_source_id, text}
    sources: tuple = ()         # each a dict {id, kind}
    report_fragment: str = ""


@dataclass(frozen=True)
class RoundTrace:
    """Round-reconstruction PROOF carrier: workspace composition + per-round burn."""
    round_idx: int
    workspace_tokens: int
    evolving_report_tokens: int
    last_tool_turn_tokens: int
    # the round's raw tool output + raw reply tokens NOT carried VERBATIM into the next round: the raw chunk
    # is fully dropped (only a <=200-char condensed reference survives) and the reply's RAW text is dropped
    # (only its PARSED claims/report_fragment persist in the durable ledger report).
    dropped_ephemera_tokens: int
    reconstructed_from_ledger: bool
    round_burn_tokens: int
    source_id: Optional[str] = None


@dataclass(frozen=True)
class CondensedReturn:
    """The §2.5 firewall artifact — the ONLY thing that crosses to the orchestrator (≤ ceiling tokens)."""
    claims: tuple
    sources: tuple
    report_fragment: str
    rounds: int
    internal_burn_tokens: int
    return_tokens: int
    budget: Optional[dict]
    breaker_tripped: bool
    truncated: bool

    def to_content(self) -> str:
        """The STRUCTURED, stable JSON string the join/orchestrator reads as the worker's ``content``."""
        return _structured_content(self.claims, self.sources, self.report_fragment)

    def token_count(self) -> int:
        """``approx_tokens(to_content())``; INVARIANT after construction: ``<= return_ceiling``."""
        return approx_tokens(self.to_content())


# ── Pure helpers ───────────────────────────────────────────────────────────

def _structured_content(claims: Sequence[dict], sources: Sequence[dict], report_fragment: str) -> str:
    """The single source of the condensed-return wire string (so the ceiling measure always matches)."""
    return json.dumps(
        {"claims": list(claims), "sources": list(sources), "report_fragment": report_fragment},
        ensure_ascii=False,
        separators=(",", ":"),
    )


# The hard floor of the condensed return: the empty structured skeleton
# (``{"claims":[],"sources":[],"report_fragment":""}``) — a ceiling below this floor cannot be honored, so
# ``enforce_return_ceiling`` returns the minimal structure and the firewall invariant becomes
# ``<= max(ceiling, floor)`` (in production the default ceiling 2000 ≫ floor, so it is exactly ``<= ceiling``).
EMPTY_SKELETON_FLOOR_TOKENS: int = approx_tokens(_structured_content((), (), ""))


def build_lean_workspace(
    question: str,
    evolving_report: str,
    last_tool_turn: str,
    *,
    instructions: str = "",
) -> list[dict]:
    """The round-N workspace = ``[system(instructions), user(question + evolving_report + last_tool_turn)]``.

    Carries ONLY the question, the durable evolving report (the ledger slice), and the LAST tool turn. It
    NEVER carries a prior-round raw chunk or raw reasoning (ephemera are dropped). PURE; mutates nothing.
    """
    if not instructions:
        instructions = (
            "You are a research subagent running one round of an IterResearch loop. You are given a question, "
            "an EVOLVING REPORT carried forward from prior rounds (durable), and a condensed reference to the "
            "LAST tool turn. Read the freshly RETRIEVED SOURCE and synthesize new findings that BUILD ON the "
            "evolving report (do not repeat it). Respond with a SINGLE JSON object: "
            '{"claims":[{"subject":"...","predicate":"...","numeric_value":<n or null>,'
            '"cited_source_id":"...","text":"..."}],"report_fragment":"..."}'
        )
    user_content = (
        f"QUESTION:\n{question}\n\n"
        f"EVOLVING REPORT (durable, from the ledger):\n{evolving_report}\n\n"
        f"LAST TOOL TURN:\n{last_tool_turn}"
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": user_content},
    ]


def condense_tool_turn(source: Optional[Source]) -> str:
    """A SMALL, fixed-shape reference to the last retrieved source (id + a short snippet).

    Carried into the NEXT round in place of the raw chunk — the raw chunk is ephemeral (dropped). PURE.
    """
    if source is None:
        return ""
    text = (getattr(source, "text", "") or "")[:200]
    kind = getattr(source, "kind", "")
    kind_value = getattr(kind, "value", kind)
    return f"[source {source.id} ({kind_value})] {text}"


def enforce_return_ceiling(
    claims: Sequence[dict],
    sources: Sequence[dict],
    report_fragment: str,
    *,
    ceiling: int,
    max_claims: int,
    max_sources: int,
) -> tuple[tuple, tuple, str, bool]:
    """Clamp the condensed return to ``<= ceiling`` tokens. Returns ``(claims, sources, fragment, truncated)``.

    (1) cap claims/sources by count; (2) progressively truncate ``report_fragment`` until the structured
    content fits; (3) if the structured claims/sources ALONE still exceed the ceiling, drop the fragment then
    pop the last source then the last claim until it fits. For any ``ceiling >= EMPTY_SKELETON_FLOOR_TOKENS``
    the result is EXACTLY ``<= ceiling``; a ceiling BELOW that floor yields the minimal empty skeleton (the
    best achievable — the floor cannot be honored). PURE.
    """
    truncated = False
    claims_list = list(claims)[:max_claims]
    if len(claims_list) < len(claims):
        truncated = True
    sources_list = list(sources)[:max_sources]
    if len(sources_list) < len(sources):
        truncated = True

    fragment = report_fragment

    def _fits(frag: str) -> bool:
        return approx_tokens(_structured_content(claims_list, sources_list, frag)) <= ceiling

    # 2) truncate the free-text fragment until it fits (binary-ish: cut 20% per step, exact-fit at the end)
    while fragment and not _fits(fragment):
        new_len = int(len(fragment) * 0.8)
        if new_len >= len(fragment):       # defensive: guarantee strict shrink for tiny lengths
            new_len = len(fragment) - 1
        fragment = fragment[: max(new_len, 0)]
        truncated = True

    # 3) structured claims/sources alone still too big → drop fragment, then pop last source/claim
    if not _fits(fragment):
        if fragment:
            truncated = True
        fragment = ""
        while sources_list and not _fits(fragment):
            sources_list.pop()
            truncated = True
        while claims_list and not _fits(fragment):
            claims_list.pop()
            truncated = True

    return (tuple(claims_list), tuple(sources_list), fragment, truncated)


def _extract_last_json_object(reply: str) -> Optional[dict]:
    """Return the LAST balanced top-level ``{...}`` object that parses to a dict, string-literal-aware.

    Mirrors ``entailment.parse_entailment_verdict``: braces inside a JSON string never change depth, and the
    model's ACTUAL answer is the FINAL object (an earlier prose-embedded object never wins). Never raises.
    """
    cleaned = (reply or "").strip()
    # strip a leading markdown fence if present
    if cleaned.startswith("```"):
        nl = cleaned.find("\n")
        if nl != -1:
            cleaned = cleaned[nl + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    candidates: list[str] = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(cleaned):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(cleaned[start : i + 1])
                    start = None
    result = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("report_fragment" in obj or "claims" in obj):
            result = obj
    return result


def _default_round_parser(reply: str, round_idx: int, source: Optional[Source]) -> RoundArtifact:
    """Tolerant round parser → ``RoundArtifact``. Never raises.

    Extracts the last JSON object carrying ``claims``/``report_fragment``; defaults a claim's
    ``cited_source_id`` to the round's retrieved ``source.id`` when omitted; falls back to the whole reply as
    ``report_fragment`` with zero claims when no JSON is present.
    """
    src_id = getattr(source, "id", None)
    src_kind = getattr(getattr(source, "kind", None), "value", getattr(source, "kind", None))
    sources = ({"id": src_id, "kind": src_kind},) if source is not None else ()

    obj = _extract_last_json_object(reply)
    if obj is None:
        return RoundArtifact(
            round_idx=round_idx,
            claims=(),
            sources=sources,
            report_fragment=(reply or "").strip(),
        )

    raw_claims = obj.get("claims")
    claims: list[dict] = []
    if isinstance(raw_claims, list):
        for c in raw_claims:
            if not isinstance(c, dict):
                continue
            c = dict(c)
            if not str(c.get("cited_source_id") or "").strip() and src_id is not None:
                c["cited_source_id"] = src_id
            claims.append(c)

    fragment = obj.get("report_fragment")
    fragment = str(fragment) if fragment is not None else ""

    return RoundArtifact(
        round_idx=round_idx,
        claims=tuple(claims),
        sources=sources,
        report_fragment=fragment,
    )


# ── The ledger-backed evolving-report store (composes the landed ledger; NO new ledger code) ──

class ReportStore(Protocol):
    """The evolving-report contract the IterResearch loop reads from / appends to each round."""

    async def read_report(self) -> str: ...
    async def append_fragment(self, artifact: RoundArtifact) -> Any: ...


class LedgerReportStore:
    """The evolving report = a LEDGER SLICE.

    ``read_report`` reads truth-at-a-commit via ``project.read_ledger_at(HEAD)`` and concatenates the committed
    report-fragment events (sorted by round) → the durable evolving report (NOT in-window). ``append_fragment``
    writes ONE report-fragment event line to ``<repo>/<ledger_dir>/events.jsonl`` and commits it via
    ``session.commit_trajectory_with_provenance`` (one commit per round-artifact). ``read_slice`` surfaces the
    incremental fragments a commit-range ADDED via ``session.ledger_slice`` (the §2.9 diff signal). Consumes
    the ledger modules unchanged; writes ONLY under ``<repo>/<ledger_dir>/``.
    """

    def __init__(self, repo: Any, *, ledger_dir: str = "ledger", report_kind: str = "report_fragment") -> None:
        """Bind the git repo path, the ledger sub-dir, and the report-fragment event kind."""
        self._repo = repo
        self._ledger_dir = ledger_dir
        self._report_kind = report_kind

    async def read_report(self) -> str:
        """Read the durable evolving report from the ledger (truth-at-HEAD); ``""`` on an empty/missing ledger."""
        try:
            truth = read_ledger_at(self._repo, "HEAD", ledger_dir=self._ledger_dir)
        except Exception as exc:  # noqa: BLE001 — a fresh/empty ledger is a normal empty report, never a crash
            logger.debug("R3 read_report: empty/unreadable ledger (%s): %s", type(exc).__name__, exc)
            return ""
        frags = [e for e in truth.get("events", []) if e.get("kind") == self._report_kind]
        frags.sort(key=lambda e: e.get("round", 0))
        return "\n\n".join(str(e.get("text") or "") for e in frags if str(e.get("text") or "").strip())

    async def read_slice(self, commit_range: str) -> list[dict]:
        """The report-fragment events a commit-range ADDED (``ledger_slice`` incremental signal); ``[]`` on error."""
        try:
            sl = ledger_slice(self._repo, commit_range, events_path=f"{self._ledger_dir}/events.jsonl")
        except Exception as exc:  # noqa: BLE001
            logger.debug("R3 read_slice(%r): %s: %s", commit_range, type(exc).__name__, exc)
            return []
        return [e for e in sl.get("events", []) if e.get("kind") == self._report_kind]

    async def append_fragment(self, artifact: RoundArtifact) -> Optional[str]:
        """Append ONE report-fragment event to ``events.jsonl`` and commit it (one commit per round-artifact)."""
        digest = hashlib.sha1(
            (str(artifact.round_idx) + (artifact.report_fragment or "")).encode("utf-8")
        ).hexdigest()[:12]
        event = {
            "id": f"rf-{artifact.round_idx}-{digest}",
            "kind": self._report_kind,
            "round": artifact.round_idx,
            "text": artifact.report_fragment,
            "claims": list(artifact.claims),
            "sources": list(artifact.sources),
        }
        ledger_path = Path(self._repo) / self._ledger_dir
        ledger_path.mkdir(parents=True, exist_ok=True)
        events_file = ledger_path / "events.jsonl"
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return commit_trajectory_with_provenance(
            self._repo,
            f"research: round {artifact.round_idx} report fragment",
            paths=[self._ledger_dir],
        )


# ── The IterResearch loop (the new primitive) ──────────────────────────────

async def run_iterresearch(
    *,
    question: str,
    retriever: Callable[[RetrieveRequest], Awaitable[Optional[Source]]],
    model_send: Callable[[list[dict], str], Awaitable[str]],
    backend: str,
    report_store: ReportStore,
    round_parser: Optional[Callable[..., RoundArtifact]] = None,
    max_rounds: int = RESEARCH_MAX_ROUNDS,
    branch_budget: Optional[dict] = None,
    return_ceiling: int = RESEARCH_RETURN_TOKEN_CEILING,
    max_claims: int = RESEARCH_MAX_CLAIMS,
    max_sources: int = RESEARCH_MAX_SOURCES,
    instructions: str = "",
    bid_key: Optional[str] = None,
) -> tuple[CondensedReturn, list[RoundTrace]]:
    """Run the IterResearch loop and return ``(CondensedReturn, [RoundTrace])``.

    Each round reconstructs a LEAN workspace from the ledger-sliced evolving report (ephemera dropped), calls
    the R1 retriever (decorrelating across rounds via ``tried_sources``), runs the floor model, and APPENDS the
    round fragment to the ledger. Internal burn is metered against the MS-4 per-branch reservation in-branch; a
    runaway round trips its OWN breaker (BIND-02) and stops the loop. The return is clamped ``<= return_ceiling``
    (the §2.5 firewall).
    """
    if not (isinstance(question, str) and question.strip()):
        raise ResearchSubagentError("question required")
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds <= 0:
        raise ResearchSubagentError("max_rounds must be a positive int")
    for name, val in (("return_ceiling", return_ceiling), ("max_claims", max_claims), ("max_sources", max_sources)):
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ResearchSubagentError(f"{name} must be a positive int")
    if model_send is None:
        raise ResearchSubagentError("model_send is required")
    if retriever is None or report_store is None:
        raise ResearchSubagentError("retriever and report_store are required")

    parser = round_parser or _default_round_parser
    bid = bid_key or ("rq-" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:12])

    tried: set[str] = set()
    cumulative_burn = 0
    last_tool_turn = ""
    traces: list[RoundTrace] = []
    all_claims: dict[tuple, dict] = {}
    all_sources: dict[str, dict] = {}
    fragments: list[str] = []
    breaker_tripped = False

    for round_idx in range(max_rounds):
        # 1) RECONSTRUCT the lean workspace from the LEDGER SLICE (durable evolving report) — ephemera dropped.
        evolving_report = await report_store.read_report()
        workspace = build_lean_workspace(question, evolving_report, last_tool_turn, instructions=instructions)
        workspace_tokens = sum(approx_tokens(m.get("content", "")) for m in workspace)

        # 2) TOOL turn — the R1 retriever (excludes tried_sources → a different source each round).
        req = RetrieveRequest(
            bid_key=bid, tried_sources=tuple(tried), constraint=None, reason_code=None, attempt=round_idx
        )
        source = await retriever(req)
        raw_tool_output = (getattr(source, "text", "") if source is not None else "") or ""
        if source is not None:
            tried.add(source.id)
            all_sources[source.id] = {
                "id": source.id,
                "kind": getattr(getattr(source, "kind", None), "value", getattr(source, "kind", None)),
            }
        tool_msg = {
            "role": "user",
            "content": (
                f"RETRIEVED SOURCE:\n{raw_tool_output}"
                if source is not None
                else "RETRIEVED SOURCE: (none — synthesize from the evolving report)"
            ),
        }

        # 3) MODEL round.
        round_messages = workspace + [tool_msg]
        reply = await model_send(round_messages, backend)
        artifact = parser(reply, round_idx, source)

        # 4) ACCOUNT — the round's REAL internal burn = the messages SENT (the raw tool output is already
        # inside ``round_messages`` via ``tool_msg``, so it is NOT added again — that would double-count it
        # and bias the BIND-02 breaker to trip early) + the reply RECEIVED.
        round_burn = (
            sum(approx_tokens(m.get("content", "")) for m in round_messages)
            + approx_tokens(reply or "")
        )
        cumulative_burn += round_burn
        dropped = approx_tokens(raw_tool_output) + approx_tokens(reply or "")
        traces.append(
            RoundTrace(
                round_idx=round_idx,
                workspace_tokens=workspace_tokens,
                evolving_report_tokens=approx_tokens(evolving_report),
                last_tool_turn_tokens=approx_tokens(last_tool_turn),
                dropped_ephemera_tokens=dropped,
                reconstructed_from_ledger=True,
                round_burn_tokens=round_burn,
                source_id=(source.id if source is not None else None),
            )
        )
        # the NEXT round carries ONLY the condensed reference (the raw chunk + raw reasoning are dropped)
        last_tool_turn = condense_tool_turn(source)

        if artifact.report_fragment:
            fragments.append(artifact.report_fragment)
        for c in artifact.claims:
            if isinstance(c, dict):
                # repr (not str) on numeric_value so None and the string "None" do NOT collapse to one key.
                key = (c.get("subject"), c.get("predicate"), repr(c.get("numeric_value")))
                all_claims[key] = c

        # 5) DURABLE APPEND — the evolving report grows in the ledger (the next round's read_report sees it).
        await report_store.append_fragment(artifact)

        # 6) MS-4 BIND-02 in-branch breaker — a runaway round trips its OWN breaker (no shared/sibling poll).
        if branch_budget:
            verdict = branch_spend_result(
                branch_budget.get("reservation", 0),
                cumulative_burn,
                trigger=branch_budget.get("trigger", OVERSPEND_TRIGGER),
            )
            if verdict.get("tripped"):
                breaker_tripped = True
                break

    # ── assemble the condensed return (the §2.5 firewall) ──
    joined = "\n\n".join(fragments)
    claims, sources, fragment, truncated = enforce_return_ceiling(
        list(all_claims.values()),
        list(all_sources.values()),
        joined,
        ceiling=return_ceiling,
        max_claims=max_claims,
        max_sources=max_sources,
    )
    budget = (
        branch_spend_result(
            branch_budget.get("reservation", 0),
            cumulative_burn,
            trigger=branch_budget.get("trigger", OVERSPEND_TRIGGER),
        )
        if branch_budget
        else None
    )
    return_tokens = approx_tokens(_structured_content(claims, sources, fragment))
    cr = CondensedReturn(
        claims=claims,
        sources=sources,
        report_fragment=fragment,
        rounds=len(traces),
        internal_burn_tokens=cumulative_burn,
        return_tokens=return_tokens,
        budget=budget,
        breaker_tripped=breaker_tripped,
        truncated=truncated,
    )
    # The firewall invariant — an EXPLICIT raise (not a bare ``assert``, which ``python -O`` strips) so the
    # ≤-ceiling guarantee is enforced under every optimization flag. For any ceiling >= the empty-skeleton
    # floor (always true in production: the default ceiling 2000 ≫ the ~12-token floor) this is exactly
    # ``<= return_ceiling``; a sub-floor ceiling can only reach the minimal skeleton, so the honest bound is
    # ``<= max(return_ceiling, floor)`` (never a spurious crash on a misconfigured tiny ceiling).
    if cr.token_count() > max(return_ceiling, EMPTY_SKELETON_FLOOR_TOKENS):
        raise ResearchSubagentError(
            f"condensed-return firewall breach: {cr.token_count()} tokens exceeds ceiling {return_ceiling}"
        )
    return cr, traces


__all__ = [
    "ResearchSubagentError",
    "RoundArtifact",
    "RoundTrace",
    "CondensedReturn",
    "ReportStore",
    "LedgerReportStore",
    "build_lean_workspace",
    "condense_tool_turn",
    "enforce_return_ceiling",
    "run_iterresearch",
]
