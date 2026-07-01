"""BoBClaw build pipeline — contract parsing + validation (pure, network-free).

Ported from ``demo_variant_b.py`` (the centerpiece "100 agents build an app" run).
The crux of contract-first building is that the apex emits ~N unit CONTRACTS as
DATA — ``{name, signature, doc, cases:[{args, expect}]}`` — which we deterministically
turn into a stub module + a pytest suite (see :mod:`core.build.skeleton`). The
parsers here turn the apex's (possibly fenced, possibly TRUNCATED) JSON into a
validated, de-duplicated contract list, and ``extract_func`` cleanly lifts a single
function definition out of a worker's reply.

Every function here is PURE and deterministic — no network, no filesystem, no LLM —
so the highest-value tests (truncation salvage, signature validation, dedup) need no
backend. The one security-relevant invariant lives in :func:`coerce_units`: a
contract ``name`` must be a bare Python identifier (``[A-Za-z_][A-Za-z0-9_]*``), which
is also what keeps a hallucinated/hostile name from ever reaching a filesystem path
(no ``..``/separators) downstream.
"""
from __future__ import annotations

import ast
import json
import re

# stdlib modules made available to every generated function (the skeleton's HEADER
# imports these, and both the plan prompt and the P1 worker prompt tell the models
# "these are already imported, do NOT add imports"). Single source of truth so the
# HEADER, the plan prompt, and the worker prompt can never drift apart.
ALLOWED_IMPORTS: tuple[str, ...] = (
    "re", "math", "json", "string", "datetime", "itertools",
    "collections", "functools", "base64", "hashlib", "hmac", "textwrap", "statistics",
)
# NOTE: ``hmac`` joins ``hashlib``/``base64`` as a pure, no-I/O, no-exec stdlib crypto
# helper — it cannot widen the sandbox's escape surface (no file/network/process). Added
# so contract-built helpers can use the CORRECT primitives (HMAC + constant-time
# compare_digest) instead of hand-rolling them.

# A contract name must be a bare identifier (also the no-path-escape guarantee).
_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

# Builtins / names a pure, stdlib-only, no-I/O impl must never reference — the static
# drift gate (P3). open = file I/O; exec/eval/compile/__import__ = dynamic code; input =
# stdin; breakpoint = debugger; getattr/setattr/delattr/vars/globals/locals + the bare
# __builtins__ name are the reflection/namespace GADGETS used to reach the above
# indirectly (the classic ``().__class__.__subclasses__()`` / ``__builtins__['open']`` /
# ``getattr(__builtins__,'open')`` escapes). We reject the NAME in any load position
# (call OR alias OR pass-through), not just a direct call. NOTE: a static denylist is
# NOT a real sandbox (see is_safe_impl) — this raises the bar against the known vectors;
# OS-level isolation is the durable boundary.
_BANNED_NAMES: frozenset[str] = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "__builtins__",
})
# Attribute accesses that walk the type / closure / globals graph to escape to builtins
# (``().__class__.__bases__[0].__subclasses__()``, ``f.__globals__``). Common benign
# dunders (__name__, __class__, __dict__, __doc__, __len__, …) are deliberately NOT here.
_ESCAPE_DUNDERS: frozenset[str] = frozenset({
    "__bases__", "__base__", "__subclasses__", "__mro__", "__globals__",
    "__builtins__", "__code__", "__closure__", "__subclasshook__",
    "__reduce__", "__reduce_ex__", "__getattribute__",
})


def unfence(text: str) -> str:
    """Strip a single ```/```json/```python code fence, returning the inner body.

    Tolerant: returns the stripped text unchanged when there is no fence. Mirrors
    the demo's ``_unfence`` — the models routinely wrap JSON / code in fences
    despite "no fences" instructions.
    """
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]
        t = re.sub(r"^(json|python)\s*", "", t.strip(), flags=re.IGNORECASE)
    return t.strip()


def valid_signature(sig: str) -> bool:
    """True iff ``sig`` is a syntactically valid ``def`` header (params only).

    Compiles ``def {sig}:\\n    pass`` — so ``f(x, y=1, *args, **kw)`` passes while
    ``f(x y)`` / ``f(`` / injection attempts fail. The deterministic skeleton renders
    ``def {signature}:`` verbatim, so an invalid signature would break the build-empty
    gate; we reject it at parse time instead.
    """
    try:
        compile(f"def {sig}:\n    pass", "<sig>", "exec")
        return True
    except SyntaxError:
        return False


def coerce_units(raw_list: list, target: int) -> list[dict]:
    """Validate + de-duplicate a raw list of contract dicts into clean contracts.

    Drops anything that is not a usable contract: non-dict entries, missing
    name/signature, non-identifier names (the no-path-escape invariant), duplicate
    names, signatures that don't start with the name / don't compile / carry a
    dangerous default-arg or annotation expression, and contracts with NO testable
    case. Keeps at most ``target`` contracts. Each survivor is normalized to
    ``{name, signature, doc, cases}`` with ``cases`` filtered to dicts carrying ``args``.

    SECURITY: the signature is rendered verbatim into a ``def`` that the build-empty
    gate IMPORTS on the host — Python evaluates default-arg/annotation expressions at
    import time, so a malicious signature (``f(x=open('/secrets').read())``) would run
    arbitrary host code. We therefore screen the signature through the SAME static gate
    (:func:`is_safe_impl`) as worker/repair impls, not just a syntax check.
    """
    units: list[dict] = []
    seen: set[str] = set()
    for u in raw_list:
        if not isinstance(u, dict):
            continue
        name, sig = u.get("name"), u.get("signature")
        if not name or not sig or name in seen:
            continue
        if not _IDENT_RE.fullmatch(str(name)):
            continue
        sig = str(sig).strip()
        if not sig.startswith(str(name)) or not valid_signature(sig):
            continue
        # Screen the signature for code-execution vectors in default args / annotations
        # (the skeleton imports the rendered def on the host) — same gate as impls.
        if not is_safe_impl(f"def {sig}:\n    pass")[0]:
            continue
        cases = [c for c in (u.get("cases") or []) if isinstance(c, dict) and "args" in c]
        # A contract with no testable case can't be honestly verified: an unfilled stub
        # would pass a trivial `assert callable` and the gate would report a FALSE green.
        if not cases:
            continue
        units.append({
            "name": name,
            "signature": sig,
            "doc": (u.get("doc") or "").strip(),
            "cases": cases,
        })
        seen.add(name)
        if len(units) >= target:
            break
    return units


def salvage_objects(text: str) -> list:
    """Recover complete top-level ``{...}`` objects from possibly-TRUNCATED JSON.

    Bracket-matches OUTSIDE of strings (so braces inside string literals don't throw
    off the depth count), ``json.loads`` each complete object, and silently drops a
    truncated tail. A skeleton response cut off mid-unit-87 still yields the first 86
    complete contracts instead of failing the whole parse.
    """
    objs: list = []
    depth = 0
    start: int | None = None
    instr = False
    esc = False
    for i, ch in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objs.append(json.loads(text[start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = None
    return objs


def parse_units(text: str, target: int) -> list[dict]:
    """Parse the apex skeleton reply into validated contracts; tolerant of truncation.

    Fast path: unfence + ``json.loads``, accepting either ``{"units": [...]}`` or a
    bare ``[...]``. Salvage path (the loads failed — usually a truncated reply): scan
    from the first ``[`` for complete ``{...}`` objects via :func:`salvage_objects`.
    Both paths funnel through :func:`coerce_units`, so the output is always clean,
    de-duplicated, and capped at ``target``.
    """
    t = unfence(text)
    try:
        data = json.loads(t)
        raw = data.get("units", data) if isinstance(data, dict) else data
        if isinstance(raw, list):
            u = coerce_units(raw, target)
            if u:
                return u
    except json.JSONDecodeError:
        pass
    # Salvage: scan the units-array contents for complete objects.
    i = t.find("[")
    return coerce_units(salvage_objects(t[i:] if i != -1 else t), target)


def is_safe_impl(source: str) -> tuple[bool, str]:
    """Static AST check that an impl honors the pure / stdlib-only / no-I/O contract.

    Returns ``(ok, reason)``. Rejects (a) any import outside :data:`ALLOWED_IMPORTS`
    — at module OR nested-in-body scope (``def f(): import os`` is caught) — and
    (b) a load-position reference to a banned name (:data:`_BANNED_NAMES`), and (c) an
    attribute access onto an escape dunder (:data:`_ESCAPE_DUNDERS`). A rejected impl
    keeps its contract STUB (surfaced as a failing unit, never silently). This
    is defense-in-depth atop the constrained subprocess env + path containment — not a
    complete Python sandbox (impossible statically), but it blocks the obvious vectors
    before the code is written or executed.

    NOT A SANDBOX: a static denylist cannot contain Python (novel gadgets exist). The
    executing subprocess is NOT filesystem/network-jailed — a bypass can read/write
    arbitrary absolute paths + spawn processes with the user's privileges (env-borne
    secrets are stripped, but on-disk secrets stay readable). OS-level isolation is the
    durable boundary, REQUIRED before executing untrusted-model output; promotion out of
    the sandbox stays always-human.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"
    allowed = set(ALLOWED_IMPORTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed:
                    return False, f"disallowed import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            # `from . import x` (level>0) has no module root in stdlib terms → reject.
            if node.level or root not in allowed:
                return False, f"disallowed import: {node.module or '(relative)'}"
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # A banned builtin/gadget referenced in any load position — direct call
            # ``open(...)``, alias ``g = open``, pass-through ``f(open)``, or the bare
            # ``__builtins__`` name — is rejected (not just a direct ``ast.Call``).
            if node.id in _BANNED_NAMES:
                return False, f"disallowed name: {node.id}"
        elif isinstance(node, ast.Attribute) and node.attr in _ESCAPE_DUNDERS:
            return False, f"disallowed attribute: {node.attr}"
    return True, ""


def build_impl_prompt(contract: dict) -> str:
    """The worker instruction to implement ONE contract (ported from the demo).

    Names the SAME stdlib modules the skeleton HEADER imports (single-sourced from
    ALLOWED_IMPORTS) and tells the worker not to add imports. Renders the contract's
    cases as the explicit must-satisfy spec; falls back to the docstring when a
    contract has no cases. Asks for ONLY the function definition so ``extract_func``
    can lift it cleanly.
    """
    name = contract["name"]
    cases = "\n".join(
        f"    {name}(" + ", ".join(repr(a) for a in c.get("args", []))
        + (f") == {c['expect']!r}" if "expect" in c else ")")
        for c in contract.get("cases", [])
    ) or "    (no explicit cases - satisfy the docstring)"
    imports_line = ", ".join(ALLOWED_IMPORTS)
    return (
        f"Implement this Python function using ONLY the standard library (these are "
        f"already imported, do NOT add imports: {imports_line}).\n\n"
        f"Signature: def {contract['signature']}:\nPurpose: {contract.get('doc', '')}\n\n"
        f"It MUST satisfy:\n{cases}\n\n"
        f"Return ONLY the complete function definition (def ...). No prose, no fences."
    )


def repair_prompt(failing_units: list[dict]) -> str:
    """The apex repair instruction over a batch of FAILING contracts (from the demo).

    Asks for a JSON map ``{name: corrected function source}`` so the repair node can
    ``extract_func`` each fix. CRITICAL: repair fixes IMPLEMENTATIONS only — it is
    never given (and never edits) the tests; the contracts' declared cases are the
    fixed spec, so a self-contradictory contract (a hallucinated expected value)
    stays red through repair and is SURFACED by the gate, never masked.
    """
    spec = [{"name": u["name"], "signature": u["signature"],
             "doc": u.get("doc", ""), "cases": u.get("cases", [])}
            for u in failing_units]
    imports_line = ", ".join(ALLOWED_IMPORTS)
    return (
        "These stdlib-only Python functions FAILED their tests. Fix each so it passes "
        f"its cases. Available imports (do not add more): {imports_line}.\n"
        "Return ONLY a JSON object mapping name -> the full corrected function source "
        "string (def ...).\n\nFAILING UNITS:\n" + json.dumps(spec, indent=1)
    )


def extract_func(code: str, name: str) -> str | None:
    """Clean source for the ``FunctionDef`` named *name*, else ``None``.

    ast-based: only the target function survives (a worker's stray imports / extra
    code / prose is dropped — the skeleton HEADER already supplies the stdlib).
    Unparseable worker output → ``None`` → the caller keeps the contract's stub, so a
    bad worker reply never breaks the build. ``ast.unparse`` normalizes formatting.
    """
    try:
        tree = ast.parse(unfence(code))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            try:
                return ast.unparse(node)
            except Exception:  # noqa: BLE001 — any unparse failure → keep the stub
                return None
    return None
