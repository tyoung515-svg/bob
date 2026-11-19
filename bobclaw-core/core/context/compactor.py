"""Claims-based compaction of closed regions of a conversation.

Each region is turned into a claim set + narrative, validated with plain code, and stored in the context DB; the
prompt assembler (core.context.assembler) reads the stored chain back in place of history that no longer fits.

What survives compaction is a set of typed, ranked, cited claims, not prose. Every claim must cite at least
one message id inside the region, and every number, identifier, URL, path, version, date and quoted string in
a claim must appear verbatim in the messages it cites. That check is deterministic: no model judges a model.
A region whose claims fail it is stored as rejected and the raw messages simply stay in the prompt.

Regions compact one after another. Later regions are shown the still-active claims of earlier ones and may
supersede them, so a correction made hours after the original statement still wins (ids are namespaced per
region: ``r2.c5``). Prompt versions: claims-v1 (first draft) and claims-v2 (importance ranking, stricter about
incidental facts, prior-claim aware).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

from core.context import store
from core.context.budget import estimate_tokens
from core.context.windows import fit_enabled, is_cloud_backend

logger = logging.getLogger(__name__)

PROMPT_VERSIONS = ("claims-v1", "claims-v2")
DEFAULT_PROMPT_VERSION = "claims-v2"
PROMPT_VERSION = DEFAULT_PROMPT_VERSION  # kept for callers/tests that import the old name
CLAIM_TYPES = (
    "decision", "constraint", "definition", "commitment", "fact",
    "correction", "open_question", "failure", "artifact", "preference",
)
NARRATIVE_KEYS = ("goal", "constraints", "decisions", "progress", "open_questions", "next_steps")
MAX_CLAIM_CHARS = 600
DEFAULT_MAX_CLAIMS = 60
HEAD_PROTECT = 2
DEFAULT_REGION_TOKENS = 12_000

LLM = Callable[[list[dict]], Awaitable[str]]


def default_prompt_version() -> str:
    chosen = os.getenv("BOB_COMPACTOR_PROMPT", DEFAULT_PROMPT_VERSION).strip()
    return chosen if chosen in PROMPT_VERSIONS else DEFAULT_PROMPT_VERSION


# ── region selection ─────────────────────────────────────────────────────────

def _tokens(message: dict) -> int:
    return estimate_tokens(str(message.get("content") or "")) + 4


def select_region(
    messages: list[dict],
    *,
    head_protect: int = HEAD_PROTECT,
    tail_tokens: int = 20_000,
    min_region_tokens: int = 4_000,
    max_region_tokens: int = 40_000,
) -> Optional[list[dict]]:
    """The oldest closed region worth compacting, or None.

    It starts after the protected opening exchange and ends before the protected recent tail, begins at a
    user message and ends at an assistant message (whole exchanges only), and is at most
    ``max_region_tokens`` (the oldest part of a larger gap is taken first; the next call takes the next part).
    """
    n = len(messages)
    if n <= head_protect + 2:
        return None
    tail_tokens = max(tail_tokens, 1)
    used, tail_start = 0, n
    while tail_start > head_protect and used < tail_tokens:
        tail_start -= 1
        used += _tokens(messages[tail_start])
    # Begin the tail at a user message by moving BACK (the tail may only grow, never vanish).
    while tail_start > head_protect and messages[tail_start].get("role") != "user":
        tail_start -= 1
    start, end = head_protect, tail_start
    while start < end and messages[start].get("role") != "user":
        start += 1
    while end > start and messages[end - 1].get("role") != "assistant":
        end -= 1
    if end - start < 2:
        return None

    total, cut = 0, start
    for i in range(start, end):
        total += _tokens(messages[i])
        cut = i + 1
        if total >= max_region_tokens:
            break
    while cut > start and messages[cut - 1].get("role") != "assistant":
        cut -= 1
    if cut == start:  # the very first exchange is bigger than the cap: take that exchange whole
        cut = next((i + 1 for i in range(start, end) if messages[i].get("role") == "assistant"), end)
    region = messages[start:cut]
    if sum(_tokens(m) for m in region) < min_region_tokens:
        return None
    return region


# ── prompt / parsing ─────────────────────────────────────────────────────────

_SCHEMA_V1 = (
    '{"claims":[{"id":"c1","type":"decision","text":"...","cites":["<message id>"],"supersedes":null}],\n'
    ' "narrative":{"goal":"...","constraints":"...","decisions":"...","progress":"...","open_questions":"...","next_steps":"..."}}'
)
_SCHEMA_V2 = (
    '{"claims":[{"id":"c1","type":"decision","importance":3,"text":"...","cites":["<message id>"],"supersedes":null}],\n'
    ' "narrative":{"goal":"...","constraints":"...","decisions":"...","progress":"...","open_questions":"...","next_steps":"..."}}'
)


def _system_prompt(version: str, max_claims: int) -> str:
    types = ", ".join(CLAIM_TYPES)
    if version == "claims-v1":
        return f"""You compact ONE closed region of a conversation into verifiable claims plus a short narrative.

Return a single JSON object and nothing else:
{_SCHEMA_V1}

Rules:
- A claim is ONE atomic statement worth remembering later. type is one of: {types}.
- cites lists the exact message ids (the token after "###") that support the claim. At least one. Only ids that appear below.
- Copy every number, identifier, path, URL, version, date and quoted string VERBATIM from the cited messages. Never round, translate or paraphrase a literal.
- If a later message corrects or replaces an earlier claim, add a "correction" claim and set "supersedes" to the earlier claim's id.
- Only state what the cited messages directly say. No speculation, no advice, no pleasantries.
- Prefer fewer, sharper claims (at most {max_claims}). Narrative fields are at most 80 words each; leave a field "" if it does not apply.
- If nothing in the region is worth keeping, return {{"claims":[],"narrative":{{}}}}.
"""
    return f"""You compact ONE closed region of a conversation into verifiable claims plus a short narrative. This is durable memory for continuing the work later, not a transcript.

Return a single JSON object and nothing else:
{_SCHEMA_V2}

What to keep (importance):
- 3 = decisions; constraints, rules and prohibitions; definitions; commitments and ownership; corrections, reversals and withdrawals; open questions and blockers the work depends on.
- 2 = identifiers, paths, URLs, versions, thresholds, dates or values that later work will need to refer to.
- 1 = anything else worth a line. Use sparingly.
- Do NOT write claims for routine status updates, counts, quantities, prices, quotes, schedules or small talk unless the speaker made a decision, set a constraint, or asked for it to be remembered.

Rules:
- A claim is ONE atomic statement. type is one of: {types}.
- cites lists the exact message ids (the token after "###") that support the claim. At least one. Only ids that appear below.
- Copy every number, identifier, path, URL, version, date and quoted string VERBATIM from the cited messages. Never round, translate or paraphrase a literal.
- If a message in this region corrects, replaces or withdraws something stated earlier, add a "correction" claim and set "supersedes" to the id of the claim it replaces: an earlier claim in your own answer, or one of the ALREADY-KNOWN CLAIMS listed below (use its id exactly as written).
- Only state what the cited messages directly say. No speculation, no advice, no pleasantries.
- At most {max_claims} claims. Narrative fields are at most 80 words each; leave a field "" if it does not apply.
- If nothing in the region is worth keeping, return {{"claims":[],"narrative":{{}}}}.
"""


def build_prompt(
    region: list[dict],
    problems: Optional[list[str]] = None,
    prior_claims: Optional[list[dict]] = None,
    version: Optional[str] = None,
    max_claims: int = DEFAULT_MAX_CLAIMS,
) -> list[dict]:
    version = version if version in PROMPT_VERSIONS else default_prompt_version()
    lines: list[str] = []
    if prior_claims and version != "claims-v1":
        lines.append("ALREADY-KNOWN CLAIMS FROM EARLIER REGIONS (read-only; do not repeat them):")
        for c in prior_claims:
            lines.append(f"- {c['id']} [{c['type']}] {c['text']}")
        lines.append("")
    lines += ["CONVERSATION REGION (oldest first):", ""]
    for m in region:
        lines.append(f"### {m['message_id']} | {m.get('role')} | {m.get('created_at') or '?'}")
        lines.append(str(m.get("content") or ""))
        lines.append("")
    user = "\n".join(lines)
    if problems:
        user += "\nYOUR PREVIOUS ANSWER WAS REJECTED. Fix these problems and answer again with the full JSON:\n- " + "\n- ".join(problems[:20])
    return [{"role": "system", "content": _system_prompt(version, max_claims)}, {"role": "user", "content": user}]


class CompactionParseError(ValueError):
    pass


def parse_response(text: str) -> tuple[list, dict]:
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.S)
    if fenced:
        body = fenced.group(1)
    else:
        a, b = body.find("{"), body.rfind("}")
        if a < 0 or b <= a:
            raise CompactionParseError("no JSON object found in the answer")
        body = body[a:b + 1]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CompactionParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("claims"), list):
        raise CompactionParseError('the JSON object has no "claims" list')
    narrative = obj.get("narrative")
    if isinstance(narrative, str):
        narrative = {"summary": narrative}
    if not isinstance(narrative, dict):
        narrative = {}
    return obj["claims"], {str(k): str(v)[:1200] for k, v in narrative.items()}


# ── deterministic validation ─────────────────────────────────────────────────

_RE_URL = re.compile(r"https?://[^\s)>\]\"']+")
_RE_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_RE_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_RE_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_RE_PATH = re.compile(r"(?:~|\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)+")
_RE_VERSION = re.compile(r"\bv?\d+(?:\.\d+){1,3}\b")
_RE_CODE = re.compile(r"`([^`\n]{2,80})`")
_RE_QUOTED = re.compile(r"\"([^\"\n]{3,80})\"")
_RE_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?(?![\w])")
_RE_TICKET = re.compile(r"\b[A-Z]{2,6}-\d{2,}\b")


def extract_literals(text: str) -> list[tuple[str, str]]:
    """Hard literals in *text* as (kind, literal). Single-digit integers are ignored (too noisy)."""
    found: list[tuple[str, str]] = []
    for kind, rx in (("url", _RE_URL), ("iso", _RE_ISO), ("uuid", _RE_UUID), ("hex", _RE_HEX),
                     ("path", _RE_PATH), ("version", _RE_VERSION), ("ticket", _RE_TICKET)):
        for m in rx.finditer(text):
            lit = m.group(0).rstrip(".,;:!?") if kind in ("url", "path") else m.group(0)   # a sentence period is not part of it
            if lit:
                found.append((kind, lit))
    found.extend(("code", m.group(1)) for m in _RE_CODE.finditer(text))
    found.extend(("quoted", m.group(1)) for m in _RE_QUOTED.finditer(text))
    for m in _RE_NUMBER.finditer(text):
        lit = m.group(0)
        if len(re.sub(r"\D", "", lit)) >= 2:
            found.append(("number", lit))
    seen: set[tuple[str, str]] = set()
    return [x for x in found if not (x in seen or seen.add(x))]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# Polarity: a prohibition that loses its NOT is the most dangerous compaction error and the one a literal check cannot
# see. If the cited text carries a negation cue, a constraint/decision/commitment/preference claim must carry a
# negation (or an equivalent such as "forbidden", "disabled", "avoid"). A contrast ("limit is 5,500, not 4,000") is
# not a prohibition and is ignored, and corrections are exempt. This is lexical, not semantic.
_RE_NEG_SOURCE = re.compile(r"\b(?:not|never|cannot|forbidden|prohibited)\b|\b\w+n['\u2019]t\b|\boff[- ]limits\b|\bstays? off\b", re.I)
_RE_NEG_CLAIM = re.compile(
    _RE_NEG_SOURCE.pattern + r"|\b(?:disallow\w*|banned?|avoid\w*|disabled?|blocked?|exclud\w+|refus\w+|reject\w*|without|"
    r"no longer|keep off|stay off|must remain off)\b", re.I)
POLARITY_TYPES = ("constraint", "decision", "commitment", "preference")
_RE_CONTRAST = re.compile(r"[,;]\s*not\b[^.;\n]*", re.I)


def polarity_dropped(claim_type: str, claim_text: str, source_text: str) -> Optional[str]:
    """The negation cue of the source that the claim lost, or None."""
    if claim_type not in POLARITY_TYPES:
        return None
    cue = _RE_NEG_SOURCE.search(_RE_CONTRAST.sub(" ", source_text))
    if cue and not _RE_NEG_CLAIM.search(claim_text):
        return cue.group(0).lower()
    return None


_RE_THOUSANDS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?%?")


def _number_forms(text: str) -> set[str]:
    """Whole-number tokens of *text* as comparable strings (thousands commas removed). A list written without
    spaces (``8443,9443``) also yields its parts. A number must match a whole token: 47 is not found in 48213."""
    forms: set[str] = set()
    for m in _RE_NUMBER.finditer(text):
        raw = m.group(0).rstrip(",")
        forms.add(raw.replace(",", ""))
        if "," in raw and not _RE_THOUSANDS.fullmatch(raw):
            forms.update(p for p in raw.split(",") if p)
    return forms


def _number_supported(lit: str, forms: set[str]) -> bool:
    raw = lit.rstrip(",")
    if raw.replace(",", "") in forms:
        return True
    return "," in raw and not _RE_THOUSANDS.fullmatch(raw) and all(p in forms for p in raw.split(",") if p)


def _mid(m: dict) -> str:
    return str(m.get("message_id") or m.get("id"))


def _importance(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return value if value in (1, 2, 3) else 2


def validate_claims(
    raw_claims: list,
    region: list[dict],
    prior_ids: Optional[Iterable[str]] = None,
    prefix: str = "",
) -> tuple[list[dict], dict]:
    """Keep the claims that are well-formed, cited inside the region, and literally supported.

    ``prior_ids`` are claims of earlier regions that this region may supersede. With a ``prefix`` the kept
    claims get namespaced ids (``r2.c5``) so ids stay unique across regions.
    """
    by_id = {_mid(m): m for m in region}
    prior = set(prior_ids or ())
    ns = (lambda x: f"{prefix}.{x}") if prefix else (lambda x: x)
    accepted: list[dict] = []
    rejected: list[dict] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    seen_text: set[tuple[str, str]] = set()
    literals_checked = literals_unsupported = 0

    def reject(cid: str, reason: str) -> None:
        rejected.append({"id": cid, "reason": reason})

    for i, c in enumerate(raw_claims):
        if not isinstance(c, dict):
            reject(f"#{i + 1}", "not_an_object")
            continue
        cid = str(c.get("id") or f"c{i + 1}")
        ctype = str(c.get("type") or "").strip().lower()
        text = str(c.get("text") or "").strip()
        cites = c.get("cites")
        if cid in seen_ids:
            reject(cid, "duplicate_id"); continue
        if ctype not in CLAIM_TYPES:
            reject(cid, f"bad_type:{ctype or 'missing'}"); continue
        if not text:
            reject(cid, "empty_text"); continue
        if len(text) > MAX_CLAIM_CHARS:
            reject(cid, "text_too_long"); continue
        if not isinstance(cites, list) or not cites or not all(isinstance(x, str) for x in cites):
            reject(cid, "no_cites"); continue
        outside = [x for x in cites if x not in by_id]
        if outside:
            reject(cid, f"cite_outside_region:{outside[0]}"); continue

        haystack = _norm(" ".join(str(by_id[x].get("content") or "") for x in cites))
        forms = _number_forms(haystack)
        bad = None
        for kind, lit in extract_literals(text):
            literals_checked += 1
            ok = _number_supported(lit, forms) if kind == "number" else (_norm(lit) in haystack)
            if not ok:
                literals_unsupported += 1
                bad = bad or lit
        if bad is not None:
            reject(cid, f"literal_unsupported:{bad}"); continue
        lost = polarity_dropped(ctype, text, haystack)
        if lost:
            reject(cid, f"polarity_dropped:{lost}"); continue

        key = (ctype, _norm(text).lower())
        if key in seen_text:
            reject(cid, "duplicate_text"); continue
        sup = c.get("supersedes")
        resolved: Optional[str] = None
        if sup:
            sup = str(sup)
            if sup in seen_ids:
                resolved = ns(sup)
            elif sup in prior:
                resolved = sup
            else:
                warnings.append(f"{cid}: supersedes {sup!r} which is not an earlier accepted claim; link dropped")
        seen_ids.add(cid)
        seen_text.add(key)
        accepted.append({"id": ns(cid), "type": ctype, "importance": _importance(c.get("importance")),
                         "text": text, "cites": list(dict.fromkeys(cites)),
                         "supersedes": resolved, "status": "VERIFIED"})

    covered = {x for c in accepted for x in c["cites"]}
    gaps = [i for i in by_id if i not in covered]
    report = {
        "claims_in": len(raw_claims), "claims_ok": len(accepted), "rejected": rejected, "warnings": warnings,
        "literals_checked": literals_checked, "literals_unsupported": literals_unsupported,
        "region_messages": len(region), "covered_messages": len(covered),
        "coverage": round(len(covered) / len(region), 3) if region else 0.0,
        "gaps": gaps[:50], "gap_count": len(gaps),
    }
    return accepted, report


def active_claims(claims: list[dict]) -> list[dict]:
    """Claims not superseded by any other claim (a chain of corrections leaves only the last one)."""
    superseded = {c["supersedes"] for c in claims if c.get("supersedes")}
    return [c for c in claims if c["id"] not in superseded]


# ── compaction ───────────────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    compaction_id: str
    status: str                      # shadow_ok | shadow_rejected | failed
    claims: list[dict] = field(default_factory=list)
    narrative: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    attempts: int = 0
    cached: bool = False
    error: Optional[str] = None
    elapsed_ms: int = 0
    region_first_id: str = ""
    region_last_id: str = ""
    region_count: int = 0
    prompt_version: str = ""


def effective_importance(claims: list[dict]) -> dict[str, int]:
    """Importance per claim id; a replacement inherits the highest importance in the chain it replaces, so a
    correction can never be filtered out while the value it corrects was shown."""
    by_id = {c["id"]: c for c in claims}
    out: dict[str, int] = {}
    for c in claims:
        best, cur, hops = c.get("importance", 2), c, 0
        while cur.get("supersedes") in by_id and hops < 100:
            cur, hops = by_id[cur["supersedes"]], hops + 1
            best = max(best, cur.get("importance", 2))
        out[c["id"]] = best
    return out


def render_claims_block(results: list[CompactionResult], min_importance: int = 1, narratives: str = "last") -> str:
    """The text a prompt would carry for these compactions (draft of the assembler's output).

    Claims superseded ANYWHERE in the chain are dropped, so the latest statement wins across regions. A region's
    narrative is prose written before later corrections existed, so by default only the newest region's narrative is
    shown (``narratives``: "last" | "all" | "none"); older ones can still state a value that a later claim replaced.
    """
    ok = [r for r in results if r.status == "shadow_ok"]
    every = [c for r in ok for c in r.claims]
    live = {c["id"] for c in active_claims(every)}
    rank = effective_importance(every)
    lines = ["[compacted history: claims extracted from earlier messages that are no longer shown; every value in a "
             "claim was checked against the message ids it cites (if you have the conv_expand tool, use it to read the "
             "originals); a claim marked (replaces ...) supersedes an earlier one]"]
    for n, r in enumerate(ok):
        lines.append(f"region {r.region_first_id}..{r.region_last_id} ({r.region_count} messages):")
        for c in r.claims:
            if c["id"] in live and rank[c["id"]] >= min_importance:
                sup = f" (replaces {c['supersedes']})" if c.get("supersedes") else ""
                lines.append(f"- {c['id']} [{c['type']}] {c['text']} [cites: {', '.join(c['cites'])}]{sup}")
        shown = narratives == "all" or (narratives == "last" and n == len(ok) - 1)
        narrative = " | ".join(f"{k}: {v}" for k, v in r.narrative.items() if v) if shown else ""
        if narrative:
            lines.append(f"narrative: {narrative}")
    return "\n".join(lines)


def default_llm(backend: Optional[str] = None) -> LLM:
    """The compactor seat: a cheap long-window model through Bob's own backend call path."""
    from core.lane_config import configured_mode, get_lane, require_backend
    chosen = (backend or get_lane().values.get("BOB_COMPACTOR_BACKEND") or get_lane().default_backend
              if configured_mode() else backend or os.getenv("BOB_COMPACTOR_BACKEND", "deepseek_v4_flash"))
    model = (get_lane().values.get("BOB_COMPACTOR_MODEL") if configured_mode()
             else os.getenv("BOB_COMPACTOR_MODEL")) or None

    async def call(messages: list[dict]) -> str:
        require_backend(chosen, model)
        from core.nodes import execute  # late: execute imports a lot

        return await execute._stream_and_collect(messages, chosen, model, None)

    call.backend = chosen  # type: ignore[attr-defined]
    return call


def _compaction_id(conversation_id: str, region: list[dict], version: str, prior: list[dict], prefix: str) -> tuple[str, str]:
    input_sha = store.sha256_text("|".join(str(m.get("content_sha256") or store.sha256_text(str(m.get("content") or "")))
                                           for m in region))
    prior_sha = store.sha256_text("|".join(f"{c['id']}:{c['text']}" for c in prior))
    return store.sha256_text(f"{version}|{conversation_id}|{input_sha}|{prior_sha}|{prefix}")[:32], input_sha


def _from_record(rec: dict) -> CompactionResult:
    return CompactionResult(
        compaction_id=rec["compaction_id"], status=rec["status"],
        claims=json.loads(rec.get("claims_json") or "[]"), narrative=json.loads(rec.get("narrative_json") or "{}"),
        report=json.loads(rec.get("validation_json") or "{}"), attempts=rec.get("attempts") or 0,
        cached=True, error=rec.get("error"), elapsed_ms=rec.get("elapsed_ms") or 0,
        region_first_id=rec.get("region_first_id") or "", region_last_id=rec.get("region_last_id") or "",
        region_count=rec.get("region_count") or 0, prompt_version=rec.get("prompt_version") or "",
    )


async def compact_region(
    conversation_id: str,
    user_id: Optional[str],
    region: list[dict],
    llm: Optional[LLM] = None,
    *,
    prior_claims: Optional[list[dict]] = None,
    claim_prefix: str = "",
    prompt_version: Optional[str] = None,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    max_attempts: int = 2,
    max_reject_ratio: float = 0.5,
    force: bool = False,
) -> CompactionResult:
    """Compact *region* once (idempotent on its content and prior claims), never raising: failure is a result."""
    version = prompt_version if prompt_version in PROMPT_VERSIONS else default_prompt_version()
    prior = list(prior_claims or [])
    compaction_id, input_sha = _compaction_id(conversation_id, region, version, prior, claim_prefix)
    if not force:
        cached = await asyncio.to_thread(store.get_compaction, compaction_id)
        if cached and cached["status"] in ("shadow_ok", "shadow_rejected"):
            return _from_record(cached)

    llm = llm or default_llm()
    started = time.monotonic()
    problems: list[str] = []
    claims: list[dict] = []
    narrative: dict = {}
    report: dict = {}
    status, error, attempts = "failed", None, 0
    tokens_in = tokens_out = 0
    for attempts in range(1, max_attempts + 1):
        prompt = build_prompt(region, problems or None, prior or None, version, max_claims)
        tokens_in += sum(estimate_tokens(m["content"]) for m in prompt)
        try:
            answer = await llm(prompt)
            tokens_out += estimate_tokens(answer)
            raw_claims, narrative = parse_response(answer)
        except CompactionParseError as exc:
            error, problems = f"parse: {exc}", [f"your answer was not usable: {exc}"]
            continue
        except Exception as exc:  # provider error, timeout, empty answer ...
            error = f"llm: {type(exc).__name__}: {exc}"
            break
        claims, report = validate_claims(raw_claims, region, [c["id"] for c in prior], claim_prefix)
        ratio = (1 - len(claims) / len(raw_claims)) if raw_claims else 0.0
        if raw_claims and (not claims or ratio > max_reject_ratio):
            error = f"validation: {len(claims)}/{len(raw_claims)} claims accepted"
            problems = [f"{r['id']}: {r['reason']}" for r in report["rejected"]]
            status = "shadow_rejected"
            continue
        status, error = "shadow_ok", None
        break

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result = CompactionResult(
        compaction_id=compaction_id, status=status, claims=claims if status == "shadow_ok" else [],
        narrative=narrative if status == "shadow_ok" else {}, report=report, attempts=attempts,
        error=error, elapsed_ms=elapsed_ms, region_first_id=_mid(region[0]), region_last_id=_mid(region[-1]),
        region_count=len(region), prompt_version=version,
    )
    record = {
        "compaction_id": compaction_id, "conversation_id": conversation_id, "user_id": user_id or "",
        "region_first_id": _mid(region[0]), "region_last_id": _mid(region[-1]), "region_count": len(region),
        "input_sha256": input_sha, "status": status, "backend": getattr(llm, "backend", ""),
        "prompt_version": version, "attempts": attempts,
        "claims_json": json.dumps(result.claims), "narrative_json": json.dumps(result.narrative),
        "validation_json": json.dumps(report), "tokens_in_est": tokens_in, "tokens_out_est": tokens_out,
        "elapsed_ms": elapsed_ms, "error": error,
    }
    try:
        await asyncio.to_thread(store.save_compaction, record)
    except Exception:
        logger.exception("context: could not store compaction %s", compaction_id)
    return result


# ── live compaction: the chain, the trigger ──────────────────────────────────
#
# Regions are compacted one after another from the protected opening onward; each sees the still-active claims of
# the earlier ones. A stored compaction is only trusted while the messages it covers are byte-identical to what the
# conversation holds now (content hash) and form one unbroken chain from the opening, so an edited, regenerated or
# truncated history simply stops being covered and the prompt falls back to the Phase 0 behaviour.

_LOCKS: dict[str, asyncio.Lock] = {}
_COOLDOWN: dict[str, float] = {}
_TASKS: set[asyncio.Task] = set()
_COOLDOWN_SECONDS = 300
_MAX_ACTIVE = 2
MAX_REGIONS_PER_RUN = 4


def _flag(name: str) -> bool:
    return os.getenv(name, "true").strip().lower() != "false"


def compactor_enabled() -> bool:
    """Background compaction. ON by default. Kill switches: BOB_COMPACTOR_ENABLED=false (or the older
    BOB_COMPACTOR_SHADOW=false) stops new compactions; BOB_CONTEXT_FIT_ENABLED=false stops everything."""
    return fit_enabled() and _flag("BOB_COMPACTOR_ENABLED") and _flag("BOB_COMPACTOR_SHADOW")


shadow_enabled = compactor_enabled  # the name the first slice used


def claims_in_prompt_enabled() -> bool:
    """Whether stored claims may stand in for omitted history in a prompt. ON by default;
    BOB_CLAIMS_IN_PROMPT=false (or BOB_CONTEXT_FIT_ENABLED=false) restores the plain Phase 0 omission note."""
    return fit_enabled() and _flag("BOB_CLAIMS_IN_PROMPT")


def soft_trigger_tokens(window_tokens: int) -> int:
    """Prompt size at which background compaction becomes worthwhile (well before the history budget is hit)."""
    raw = os.getenv("BOB_COMPACTOR_SOFT_TOKENS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if os.getenv("BOB_CTX_PROFILE", "").strip().lower() == "long":
        return int(window_tokens * 0.70)
    return min(int(window_tokens * 0.25), 200_000)


def region_params(window_tokens: int) -> dict:
    raw = os.getenv("BOB_COMPACTOR_REGION_TOKENS", "").strip()
    region = int(raw) if raw.isdigit() and int(raw) >= 2_000 else DEFAULT_REGION_TOKENS
    return {
        "head_protect": 0,                       # the caller slices the list at the end of the chain
        "tail_tokens": min(max(int(window_tokens * 0.10), 8_000), 120_000),
        "min_region_tokens": min(4_000, region),
        "max_region_tokens": region,
    }


def chain_of(rows: list[tuple[str, str]], records: list[dict], version: Optional[str] = None,
             start: int = HEAD_PROTECT) -> list[dict]:
    """The unbroken chain of trusted compactions over *rows* (``(message_id, content_sha256)`` oldest first).

    Each item is ``{"first": i, "last": j, "rec": stored_record}`` with row indexes. A record is trusted when it is
    ``shadow_ok``, from the current prompt version, covers exactly the rows it claims (same count, same content
    hash) and starts where the previous one ended. The first stored record for a region wins.
    """
    version = version or default_prompt_version()
    pos = {mid: i for i, (mid, _sha) in enumerate(rows)}
    candidates: list[tuple[int, str, int, dict]] = []
    for rec in records:
        if rec.get("status") != "shadow_ok" or rec.get("prompt_version") != version:
            continue
        a, b = pos.get(rec.get("region_first_id")), pos.get(rec.get("region_last_id"))
        if a is None or b is None or b < a or b - a + 1 != rec.get("region_count"):
            continue
        if store.sha256_text("|".join(sha for _mid, sha in rows[a:b + 1])) != rec.get("input_sha256"):
            continue
        candidates.append((a, str(rec.get("created_ts") or ""), b, rec))
    candidates.sort(key=lambda t: (t[0], t[1]))
    chain: list[dict] = []
    nxt = start
    for a, _ts, b, rec in candidates:
        if a == nxt:
            chain.append({"first": a, "last": b, "rec": rec})
            nxt = b + 1
        elif a > nxt:
            break
    return chain


def _chain_claims(chain: list[dict]) -> list[dict]:
    return [c for item in chain for c in json.loads(item["rec"].get("claims_json") or "[]")]


async def run_compaction(
    conversation_id: str, user_id: Optional[str], window_tokens: int, llm: Optional[LLM] = None
) -> list[CompactionResult]:
    """Extend the conversation's chain by up to MAX_REGIONS_PER_RUN regions. Never raises; returns the new results."""
    lock = _LOCKS.setdefault(conversation_id, asyncio.Lock())
    if lock.locked() or time.monotonic() < _COOLDOWN.get(conversation_id, 0.0):
        return []
    version = default_prompt_version()
    results: list[CompactionResult] = []
    async with lock:
        for _ in range(MAX_REGIONS_PER_RUN):
            messages = await asyncio.to_thread(store.list_messages, conversation_id, user_id)
            records = await asyncio.to_thread(store.list_compactions, conversation_id, user_id or "")
            rows = [(m["message_id"], m["content_sha256"]) for m in messages]
            chain = chain_of(rows, records, version)
            nxt = chain[-1]["last"] + 1 if chain else HEAD_PROTECT
            region = select_region(messages[nxt:], **region_params(window_tokens))
            if not region or region[0] is not messages[nxt]:      # nothing closed yet, or it would leave a gap
                break
            result = await compact_region(
                conversation_id, user_id, region, llm,
                prior_claims=active_claims(_chain_claims(chain)), claim_prefix=f"r{len(chain) + 1}",
                prompt_version=version,
            )
            results.append(result)
            if result.status == "failed":
                _COOLDOWN[conversation_id] = time.monotonic() + _COOLDOWN_SECONDS
            if result.status != "shadow_ok":
                break
        logger.info(
            "context: compaction conv=%s regions=%d statuses=%s claims=%s",
            conversation_id, len(results), [r.status for r in results], [r.report.get("claims_ok") for r in results],
        )
    return results


def maybe_schedule_compaction(state: dict) -> bool:
    """Called after a turn is recorded: start a background compaction run if it is allowed and worthwhile.

    Only conversations whose backend is a known CLOUD backend are compacted: a conversation kept on a local model
    (or on an unknown/unset backend, where routing could land anywhere) is never sent to the cloud compactor seat.
    """
    if not compactor_enabled():
        return False
    conversation_id, user_id = state.get("conversation_id"), state.get("user_id")
    fit = state.get("history_fit") or {}
    window = int(fit.get("window_tokens") or 0)
    if not conversation_id or not window or not is_cloud_backend(fit.get("backend")):
        return False
    if int(fit.get("tokens_est_before") or 0) < soft_trigger_tokens(window):
        return False
    if len([t for t in _TASKS if not t.done()]) >= _MAX_ACTIVE:
        return False
    try:
        task = asyncio.get_running_loop().create_task(run_compaction(conversation_id, user_id, window))
    except RuntimeError:
        return False
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return True


maybe_schedule_shadow = maybe_schedule_compaction  # the name the first slice (and the turn-event hook) used
run_shadow = run_compaction
