"""BoBClaw build pipeline P0 — contract parsing + deterministic skeleton tests.

Network-free. These are the highest-value deterministic tests for the build loop:
truncation salvage, signature validation, dedup, the no-path-escape identifier
invariant, the ast impl extractor, and the build-empty gate over a real (tiny)
skeleton subprocess. No backend / LLM involved.
"""
from __future__ import annotations

import json

from core.build import contracts, skeleton
from core.build.skeleton import build_empty_ok, collect_tests, render_test, write_app


# ── valid_signature ──────────────────────────────────────────────────────────

def test_valid_signature_accepts_real_headers():
    assert contracts.valid_signature("f(x, y)")
    assert contracts.valid_signature("f(x, y=1, *args, **kw)")
    assert contracts.valid_signature("f()")


def test_valid_signature_rejects_garbage():
    assert not contracts.valid_signature("f(x y)")      # missing comma
    assert not contracts.valid_signature("f(")          # unbalanced
    assert not contracts.valid_signature("f(x):pass")   # not a bare header


# ── coerce_units: validation, dedup, the identifier invariant ────────────────

def _u(name, sig, **kw):
    # Default to one case so the contract survives coerce_units (which now requires a
    # testable case); pass cases=[] explicitly to exercise the no-cases path.
    return {"name": name, "signature": sig, "doc": kw.get("doc", ""),
            "cases": kw.get("cases", [{"args": [1], "expect": 1}])}


def test_coerce_dedups_by_name():
    raw = [_u("foo", "foo(x)"), _u("foo", "foo(y)")]
    out = contracts.coerce_units(raw, 10)
    assert [u["name"] for u in out] == ["foo"]


def test_coerce_rejects_non_identifier_names():
    # The no-path-escape invariant: a name with separators / leading digit is dropped.
    raw = [_u("1bad", "f(x)"), _u("a-b", "f(x)"), _u("../evil", "f(x)"),
           _u("good", "good(x)")]
    out = contracts.coerce_units(raw, 10)
    assert [u["name"] for u in out] == ["good"]


def test_coerce_requires_signature_to_start_with_name():
    raw = [_u("foo", "bar(x)")]          # signature names a different function
    assert contracts.coerce_units(raw, 10) == []


def test_coerce_rejects_invalid_signature():
    raw = [_u("foo", "foo(x y)")]        # compiles to a SyntaxError
    assert contracts.coerce_units(raw, 10) == []


def test_coerce_skips_non_dicts_and_missing_fields():
    raw = ["nope", 42, {}, {"name": "foo"}, {"signature": "bar(x)"}, _u("ok", "ok()")]
    out = contracts.coerce_units(raw, 10)
    assert [u["name"] for u in out] == ["ok"]


def test_coerce_filters_cases_to_arg_dicts():
    raw = [_u("foo", "foo(x)", cases=[{"args": [1], "expect": 1}, {"no_args": 1},
                                      "junk", {"args": [2]}])]
    out = contracts.coerce_units(raw, 10)
    assert out[0]["cases"] == [{"args": [1], "expect": 1}, {"args": [2]}]


def test_coerce_caps_at_target():
    raw = [_u(f"f{i}", f"f{i}()") for i in range(5)]
    assert len(contracts.coerce_units(raw, 2)) == 2


# ── salvage_objects: brace-matching outside strings, truncated tail ──────────

def test_salvage_ignores_braces_inside_strings():
    objs = contracts.salvage_objects('{"a": "x}y{z"}')
    assert objs == [{"a": "x}y{z"}]


def test_salvage_drops_truncated_tail():
    objs = contracts.salvage_objects('{"a": 1}{"b": 2}{"c":')
    assert objs == [{"a": 1}, {"b": 2}]


def test_salvage_handles_escaped_quotes():
    objs = contracts.salvage_objects(r'{"a": "he said \"hi\""}')
    assert objs == [{"a": 'he said "hi"'}]


# ── parse_units: clean, bare-list, fenced, and the truncation path ───────────

def test_parse_units_clean_object():
    text = json.dumps({"units": [_u("add", "add(a, b)", cases=[{"args": [1, 2], "expect": 3}])]})
    out = contracts.parse_units(text, 10)
    assert len(out) == 1 and out[0]["name"] == "add"


def test_parse_units_bare_list():
    text = json.dumps([_u("add", "add(a, b)")])
    out = contracts.parse_units(text, 10)
    assert [u["name"] for u in out] == ["add"]


def test_parse_units_strips_code_fence():
    text = "```json\n" + json.dumps({"units": [_u("add", "add(a)")]}) + "\n```"
    out = contracts.parse_units(text, 10)
    assert [u["name"] for u in out] == ["add"]


def test_parse_units_salvages_truncated_reply():
    # The apex got cut off mid-second-unit: the first complete contract survives.
    text = (
        '{"units": [\n'
        '  {"name": "add", "signature": "add(a, b)", "doc": "sum", '
        '"cases": [{"args": [1, 2], "expect": 3}]},\n'
        '  {"name": "mul", "signature": "mul(a, b)", "doc": "prod", "cases": [{"args": [2, 3], "exp'
    )
    out = contracts.parse_units(text, 10)
    assert [u["name"] for u in out] == ["add"]


# ── extract_func: ast lift, noise drop, fail-soft ───────────────────────────

def test_extract_func_lifts_target_only():
    code = "import os\ndef foo(x):\n    return x + 1\nprint('side effect')\n"
    src = contracts.extract_func(code, "foo")
    assert src is not None
    assert "def foo" in src
    assert "import os" not in src and "print" not in src


def test_extract_func_returns_none_on_unparseable():
    assert contracts.extract_func("def foo(:\n  pass", "foo") is None


def test_extract_func_returns_none_when_name_absent():
    assert contracts.extract_func("def bar():\n    return 1\n", "foo") is None


def test_extract_func_handles_fenced_reply():
    code = "```python\ndef foo():\n    return 1\n```"
    src = contracts.extract_func(code, "foo")
    assert src is not None and "def foo" in src


# ── render_test: cases → asserts; no cases → callable ────────────────────────

def test_render_test_with_cases_emits_equality_assert():
    t = render_test(_u("add", "add(a, b)", cases=[{"args": [1, 2], "expect": 3}]))
    assert "def test_add" in t
    assert "add(1, 2) == 3" in t


def test_render_test_without_cases_asserts_callable():
    t = render_test(_u("noop", "noop()", cases=[]))
    assert "assert callable(noop)" in t


def test_coerce_rejects_no_cases_contract():
    # A contract with no testable case can't be honestly verified -> dropped (else an
    # unfilled stub would pass `assert callable` and the gate would report false green).
    assert contracts.coerce_units([{"name": "x", "signature": "x()", "cases": []}], 10) == []


def test_coerce_rejects_dangerous_signature():
    # RCE guard: a default-arg / annotation expression that executes is rejected — the
    # signature is rendered into a def the build-empty gate IMPORTS on the host.
    for sig in ["solve(x=open('/etc/passwd').read())",
                "solve(x=__import__('os').system('id'))",
                "solve(x: open('x')=1)"]:
        assert contracts.coerce_units(
            [{"name": "solve", "signature": sig, "cases": [{"args": [1], "expect": 1}]}], 10) == [], sig


def test_render_test_preserves_wrong_expect_verbatim():
    # The gate must SURFACE a bad spec, never silently auto-fix it: a contradictory
    # expected value is rendered VERBATIM (no normalization) so the later verify gate
    # catches it (the demo's 1 red was a hallucinated expect the gate correctly caught).
    t = render_test(_u("addtwo", "addtwo(a, b)", cases=[{"args": [1, 2], "expect": 999}]))
    assert "addtwo(1, 2) == 999" in t          # the WRONG value, untouched


# ── skeleton write + build-empty gate (real subprocess, deterministic) ───────

def _two_contracts():
    return [
        _u("addtwo", "addtwo(a, b)", doc="sum", cases=[{"args": [1, 2], "expect": 3}]),
        _u("neg", "neg(x)", doc="negate", cases=[{"args": [5], "expect": -5}]),
    ]


def test_write_app_emits_package_tests_and_cli(tmp_path):
    write_app(tmp_path, _two_contracts(), {})
    pkg = tmp_path / skeleton.PACKAGE_NAME
    assert (pkg / "functions.py").exists()
    assert (pkg / "__init__.py").exists()
    assert (pkg / "cli.py").exists()
    assert (tmp_path / "tests" / "test_functions.py").exists()
    fns = (pkg / "functions.py").read_text(encoding="utf-8")
    assert "raise NotImplementedError" in fns          # empty skeleton = stubs
    assert "import re, math, json" in fns               # the HEADER


def test_skeleton_builds_empty_and_collects(tmp_path):
    write_app(tmp_path, _two_contracts(), {})
    assert build_empty_ok(tmp_path) is True
    assert collect_tests(tmp_path) == 2


def test_skeleton_with_impl_still_builds(tmp_path):
    impls = {"addtwo": "def addtwo(a, b):\n    return a + b"}
    write_app(tmp_path, _two_contracts(), impls)
    fns = (tmp_path / skeleton.PACKAGE_NAME / "functions.py").read_text(encoding="utf-8")
    assert "return a + b" in fns
    assert build_empty_ok(tmp_path) is True


def test_build_empty_fails_loud_on_unimportable_skeleton(tmp_path):
    # A signature that COMPILES (valid_signature/coerce admit it — compile() is a
    # syntax-only check) but NameErrors at IMPORT time (undefined default arg). The
    # build-empty gate must return False so the node can fail loud, never silently
    # proceed — the negative case is the whole point of a fail-loud gate.
    bad = _u("bad", "bad(x=__nope_undefined__)")
    assert contracts.coerce_units([bad], 10)        # parser admits it
    write_app(tmp_path, [bad], {})
    assert build_empty_ok(tmp_path) is False


def test_write_app_does_not_autofix_a_bad_spec(tmp_path):
    # A wrong expect survives verbatim into the generated suite AND the skeleton still
    # builds empty — the bad spec is left for the (P2) verify gate to surface, never
    # masked by editing the test here.
    units = [_u("addtwo", "addtwo(a, b)", cases=[{"args": [1, 2], "expect": 999}])]
    write_app(tmp_path, units, {})
    suite = (tmp_path / "tests" / "test_functions.py").read_text(encoding="utf-8")
    assert "== 999" in suite
    assert build_empty_ok(tmp_path) is True
