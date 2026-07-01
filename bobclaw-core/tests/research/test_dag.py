"""MS2-R6 — unit tests for the git-DAG wiring (`core/research/dag.py`).

PURE(-ish): drives a REAL throwaway git repo via the `tmp_path` fixture (git subprocess is local + deterministic —
the landed ledger tests do the same); NO network, NO real model, NO live corpus. Round inputs are injected fakes.
"""

import importlib.util
import subprocess

import pytest

from core.research.dag import (
    ResearchDagError,
    RoundInputs,
    RoundCommit,
    DagResult,
    ResearchDag,
    verdicts_from_round,
    round_inputs_from_converge,
)
from core.ledger.project import read_ledger_at
from core.ledger.gitdag import head_sha, current_branch


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8")


def init_repo(tmp_path):
    """A fresh throwaway git repo with an empty ledger/events.jsonl initial commit (NOT a live corpus)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "r6@test.local")
    _git(repo, "config", "user.name", "R6 Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "ledger").mkdir()
    (repo / "ledger" / "events.jsonl").write_text("", encoding="utf-8")
    _git(repo, "add", "ledger")
    _git(repo, "commit", "-q", "-m", "init ledger")
    return repo


def mk_claim(subj, pred, num, **kw):
    c = {
        "subject": subj,
        "predicate": pred,
        "numeric_value": str(num),
        "cited_source_id": kw.get("cid"),
        "text": kw.get("text", f"{subj} {pred} {num}"),
    }
    c["bid_key"] = kw.get("bid_key", f"{subj}|{pred}|{num}")
    return c


def mk_inputs(claims, surviving_keys, cnv=(), asserter="deepseek_v4_flash", escalated=False):
    return RoundInputs(
        claims=tuple(claims),
        surviving_keys=tuple(surviving_keys),
        could_not_verify=tuple(cnv),
        asserter_backend=asserter,
        budget_escalated=escalated,
    )


class FakeConverge:
    """A duck-typed R5 ConvergeResult for round_inputs_from_converge (no converge import)."""

    def __init__(self, surviving_keys, could_not_verify, asserter_backend, budget_bound=False):
        self.surviving_keys = tuple(surviving_keys)
        self.could_not_verify = tuple(could_not_verify)
        self.asserter_backend = asserter_backend
        self.budget_bound = budget_bound


# ── 1 ────────────────────────────────────────────────────────────────────────
def test_verdicts_from_round():
    surviving = ["a"]
    cnv = [
        {"bid_key": "b", "kind": "refuted", "reasons": ["x"]},
        {"bid_key": "c", "kind": "[UNVERIFIED: EXHAUSTED_SEARCH]", "reasons": ["exhausted"]},
    ]
    verdicts = verdicts_from_round(surviving, cnv)
    by = {v["bid_key"]: v for v in verdicts}
    assert by["a"]["verified"] is True and by["a"]["exhausted"] is False
    assert by["b"]["verified"] is False and by["b"]["exhausted"] is False
    assert by["c"]["verified"] is False and by["c"]["exhausted"] is True
    # de-dup: surviving wins over a stale could-not-verify entry for the same bid_key
    dedup = verdicts_from_round(["a"], [{"bid_key": "a", "kind": "refuted", "reasons": ["dup"]}])
    assert len([v for v in dedup if v["bid_key"] == "a"]) == 1
    assert dedup[0]["verified"] is True


# ── 2 ────────────────────────────────────────────────────────────────────────
def test_round_inputs_from_converge():
    cr = FakeConverge(["a"], [{"bid_key": "b", "kind": "refuted", "reasons": ["r"]}],
                      "deepseek_v4_flash", budget_bound=True)
    ri = round_inputs_from_converge(cr, [mk_claim("A", "is", 1, bid_key="a"), mk_claim("B", "is", 2, bid_key="b")])
    assert ri.surviving_keys == ("a",)
    assert ri.could_not_verify == cr.could_not_verify   # the contested-claim channel round-trips (routes REVERT)
    assert ri.asserter_backend == "deepseek_v4_flash"
    assert ri.budget_escalated is True
    assert {c["bid_key"] for c in ri.claims} == {"a", "b"}


# ── 3 ────────────────────────────────────────────────────────────────────────
def test_branch_per_round_and_one_commit_per_trajectory(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="thesis")
    res = dag.run([
        mk_inputs([mk_claim("A", "is", 1, bid_key="a_key")], ["a_key"]),
        mk_inputs([mk_claim("B", "is", 2, bid_key="b_key")], ["b_key"]),
    ])
    branches = _git(repo, "branch", "--list").stdout
    assert "research/20260701-thesis-r0" in branches
    assert "research/20260701-thesis-r1" in branches
    for i in range(2):
        log = _git(repo, "log", f"research/20260701-thesis-r{i}", "--oneline",
                   "--grep", f"round {i} synthesis").stdout.strip()
        assert len(log.splitlines()) == 1, f"round {i}: expected exactly 1 synthesis commit, got {log!r}"
        assert res.rounds[i].artifact_sha is not None


# ── 4 ────────────────────────────────────────────────────────────────────────
def test_merge_is_synthesis(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="merge_test")
    inputs = mk_inputs(
        [mk_claim("A", "is", 1, bid_key="alive"), mk_claim("B", "is", 2, bid_key="dead")],
        ["alive"],
        cnv=[{"bid_key": "dead", "kind": "refuted", "reasons": ["wrong"]}],
    )
    rc = dag.commit_round(0, inputs, seed_ref=head_sha(repo))
    truth = read_ledger_at(repo, rc.merge_sha)
    assert "alive" in truth["claims"]         # synthesized into the merged tree
    assert "dead" not in truth["claims"]       # contested -> reverted -> absent from the report
    parents = _git(repo, "rev-list", "--parents", "-n1", rc.merge_sha).stdout.strip().split()
    assert len(parents) == 3 and parents[0] == rc.merge_sha   # a true synthesis merge (two parents)


# ── 5 ────────────────────────────────────────────────────────────────────────
def test_contested_revert_with_reason(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="x")
    inputs = mk_inputs(
        [mk_claim("study", "enrolled", 100, bid_key="rob"), mk_claim("study", "enrolled", 250, bid_key="con")],
        ["rob"],
        cnv=[{"bid_key": "con", "kind": "refuted", "reasons": ["source states 100"]}],
    )
    rc = dag.commit_round(0, inputs, seed_ref=head_sha(repo))
    assert rc.decision["decision"] == "REVERT"
    assert len(rc.contested) == 1
    entry = rc.contested[0]
    assert entry["assert_sha"] is not None and entry["revert_sha"] is not None
    subject = _git(repo, "show", "-s", "--format=%s", entry["revert_sha"]).stdout.strip()
    assert subject.startswith('Revert "research: contested claim con'), subject
    assert "REVERTED: source states 100" in subject   # the contest reason survives onto the wire
    truth = read_ledger_at(repo, rc.merge_sha)
    assert "rob" in truth["claims"]
    assert "con" not in truth["claims"]


# ── 6 ────────────────────────────────────────────────────────────────────────
def test_all_verified_fast_forward_no_revert(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="ffwd")
    rc = dag.commit_round(0, mk_inputs([mk_claim("X", "is", 42, bid_key="a")], ["a"]), seed_ref=head_sha(repo))
    assert rc.decision["decision"] == "FAST_FORWARD"
    assert rc.contested == ()
    assert _git(repo, "log", rc.branch, "--oneline", "--grep", "Revert").stdout.strip() == ""
    # merge=synthesis is structural even on the all-verified path (merge_synthesis is --no-ff -> 2 parents)
    parents = _git(repo, "rev-list", "--parents", "-n1", rc.merge_sha).stdout.strip().split()
    assert len(parents) == 3
    assert "a" in read_ledger_at(repo, rc.merge_sha)["claims"]   # the survivor IS carried into the merged tree


# ── 7 ────────────────────────────────────────────────────────────────────────
def test_rebranch_from_merge(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="rebr")
    res = dag.run([
        mk_inputs([mk_claim("A", "is", 1, bid_key="a")], ["a"]),
        mk_inputs([mk_claim("B", "is", 2, bid_key="b")], ["b"]),
    ])
    rc0, rc1 = res.rounds
    assert rc1.seed_ref == rc0.merge_sha                       # round 1 re-branches from round 0's merge
    is_ancestor = _git(repo, "merge-base", "--is-ancestor", rc0.merge_sha, rc1.branch).returncode == 0
    assert is_ancestor
    assert "a" in dag.seed(rc0.merge_sha)["claims"]            # the evolving report carried forward
    final = read_ledger_at(repo, res.final_ref)
    assert "a" in final["claims"] and "b" in final["claims"]   # cumulative report


# ── 8 ────────────────────────────────────────────────────────────────────────
def test_blame_queryable(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="blame")
    dag.run([mk_inputs(
        [mk_claim("study", "enrolled", 100, bid_key="rob"), mk_claim("study", "enrolled", 250, bid_key="con")],
        ["rob"],
        cnv=[{"bid_key": "con", "kind": "refuted", "reasons": ["wrong"]}],
        asserter="deepseek_v4_flash",
    )])
    b = dag.blame("rob")
    assert b and b[0]["asserter"] == "deepseek_v4_flash" and b[0]["round"] == 0
    assert all(e["claim"] == "rob" for e in b)   # blame filters by the queried claim (not a global dump)
    # a contested claim (asserted then reverted) is STILL blame-traceable (its event rode the durable artifact commit)
    b_con = dag.blame("con")
    assert b_con and all(e["claim"] == "con" for e in b_con)
    assert b_con[0]["round"] == 0 and b_con[0]["asserter"] == "deepseek_v4_flash"


# ── 9 ────────────────────────────────────────────────────────────────────────
def test_budget_escalate(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="escalate")
    rc = dag.commit_round(0, mk_inputs([mk_claim("A", "is", 1, bid_key="a_key")], ["a_key"], escalated=True),
                          seed_ref=head_sha(repo))
    assert rc.decision["decision"] == "ESCALATE"
    assert rc.escalated is True
    assert rc.contested == ()
    assert "a_key" in read_ledger_at(repo, rc.merge_sha)["claims"]   # survivor still synthesized, cost surfaced
    parents = _git(repo, "rev-list", "--parents", "-n1", rc.merge_sha).stdout.strip().split()
    assert len(parents) == 3                                          # a real synthesis merge even on ESCALATE
    # ESCALATE is surfaced at the run level too (the caller can gate on it)
    d2 = tmp_path / "d2"
    d2.mkdir()
    res = ResearchDag(init_repo(d2), date="20260701", slug="esc2").run(
        [mk_inputs([mk_claim("A", "is", 1, bid_key="a")], ["a"], escalated=True)])
    assert res.escalated is True


# ── 10 ───────────────────────────────────────────────────────────────────────
def test_no_survivor_round(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="no_surv")
    rc = dag.commit_round(
        0,
        mk_inputs([mk_claim("C", "is", 3, bid_key="only_con")], [],
                  cnv=[{"bid_key": "only_con", "kind": "refuted", "reasons": ["bad"]}]),
        seed_ref=head_sha(repo),
    )
    # the assertion log still records the contested claim (could-not-verify stays queryable) ...
    assert rc.artifact_sha is not None
    assert len(rc.contested) == 1 and rc.contested[0]["revert_sha"] is not None
    # ... but the contested claim is NOT in the projected report tree (assert on the REAL merge, no fallback).
    assert rc.merge_sha is not None
    assert "only_con" not in read_ledger_at(repo, rc.merge_sha)["claims"]
    # still blame-traceable with the CORRECT round + asserter (not a bare truthy check)
    bl = dag.blame("only_con")
    assert bl and all(e["claim"] == "only_con" for e in bl)
    assert bl[0]["round"] == 0 and bl[0]["asserter"] == "deepseek_v4_flash"


# ── 11 ───────────────────────────────────────────────────────────────────────
def test_dagresult_cumulative(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="cumul")
    res = dag.run([
        mk_inputs([mk_claim("A", "is", 1, bid_key="a_key"), mk_claim("B", "is", 2, bid_key="b_key")],
                  ["a_key"], cnv=[{"bid_key": "b_key", "kind": "refuted", "reasons": ["r0"]}]),
        mk_inputs([mk_claim("C", "is", 3, bid_key="c_key")], ["c_key"]),
    ])
    assert set(res.surviving_keys) == {"a_key", "c_key"}
    contested_b = [e for e in res.could_not_verify if e["bid_key"] == "b_key"]
    assert contested_b and contested_b[0]["round"] == 0
    assert set(read_ledger_at(repo, res.final_ref)["claims"].keys()) == {"a_key", "c_key"}


# ── 12 ───────────────────────────────────────────────────────────────────────
def test_construction_validation(tmp_path):
    repo = init_repo(tmp_path)
    with pytest.raises(ResearchDagError):
        ResearchDag("", date="20260701", slug="x")
    with pytest.raises(ResearchDagError):
        ResearchDag(repo, date="", slug="x")
    with pytest.raises(ResearchDagError):
        ResearchDag(repo, date="20260701", slug="")
    dag = ResearchDag(repo, date="20260701", slug="neg")
    with pytest.raises(ResearchDagError):
        dag.commit_round(-1, mk_inputs([mk_claim("A", "is", 1, bid_key="a")], ["a"]), seed_ref=head_sha(repo))
    # fail closed: a surviving_key with no matching claim (the merge gate would accept a survivor absent from the tree)
    with pytest.raises(ResearchDagError):
        dag.commit_round(0, mk_inputs([mk_claim("A", "is", 1, bid_key="a")], ["a", "ghost"]), seed_ref=head_sha(repo))


# ── 13 ───────────────────────────────────────────────────────────────────────
def test_no_new_ledger_code_source_guard():
    """ast-based guard: dag.py COMPOSES the landed ledger primitives; it never shells git or re-implements one."""
    import ast
    import re as _re

    spec = importlib.util.find_spec("core.research.dag")
    assert spec is not None
    source = spec.loader.get_source(spec.name)
    tree = ast.parse(source)

    # any git-shelling / third-party git vector (GitPython imports as `git`; add dulwich/pygithub/pygit2)
    forbidden = {"subprocess", "git", "pygit2", "dulwich", "pygithub", "sh", "plumbum"}
    import_froms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                assert n.name.split(".")[0] not in forbidden, f"forbidden import: {n.name}"
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            assert top not in forbidden, f"forbidden import-from: {node.module}"
            import_froms.add(node.module or "")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr not in ("system", "popen") and not node.attr.startswith("spawn"), \
                f"forbidden os.{node.attr} (shelling git)"
    # the composition imports are REAL ImportFrom nodes (a comment mentioning them cannot satisfy this)
    for mod in ("core.ledger.gitdag", "core.ledger.session", "core.ledger.project",
                "core.ledger.mergegate", "core.ledger.gitlog"):
        assert mod in import_froms, f"missing composition import: {mod}"
    # no _git-style git driver defined in-module
    assert not _re.search(r"def\s+_?git", source)


# ── 14: fail-closed on a revert failure (the r1 BLOCKER regression) ───────────
def test_revert_failure_fails_closed(tmp_path, monkeypatch):
    """If revert_claim fails, the round MUST NOT merge an un-reverted contested claim into the report."""
    import core.research.dag as dagmod
    from core.ledger.gitdag import GitError

    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="failclosed")
    base_branch = current_branch(repo)
    base_before = head_sha(repo)
    monkeypatch.setattr(dagmod, "revert_claim", lambda *a, **k: (_ for _ in ()).throw(GitError("planted revert conflict")))
    inputs = mk_inputs(
        [mk_claim("study", "enrolled", 100, bid_key="rob"), mk_claim("study", "enrolled", 250, bid_key="con")],
        ["rob"],
        cnv=[{"bid_key": "con", "kind": "refuted", "reasons": ["source states 100"]}],
    )
    with pytest.raises(ResearchDagError):
        dag.commit_round(0, inputs, seed_ref=head_sha(repo))
    # the base/report branch NEVER advanced (no merge happened) -> the un-reverted contested claim never reached it
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == base_before
    assert read_ledger_at(repo, base_before)["claims"] == {}


# ── 16: a prior-round survivor re-contested in a later round is REMOVED (the r3c1min BLOCKER) ──
def test_prior_survivor_recontested_is_removed(tmp_path):
    """A claim that survived an earlier round, then re-examined and CONTESTED in a later round, must be REMOVED
    from the report (not left in via a no-diff no-op) — the core refute-and-vote / re-branch-from-merge path."""
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="recontest")
    claim = mk_claim("study", "enrolled", 100, bid_key="x")
    res = dag.run([
        mk_inputs([claim], ["x"]),                                                        # round 0: x survives
        mk_inputs([claim], [], cnv=[{"bid_key": "x", "kind": "refuted", "reasons": ["later refuted"]}]),  # round 1: contested
    ])
    rc0, rc1 = res.rounds
    assert "x" in read_ledger_at(repo, rc0.merge_sha)["claims"]        # present after round 0
    assert "x" not in read_ledger_at(repo, rc1.merge_sha)["claims"]    # REMOVED after round 1 (Default-FAIL)
    assert "x" not in read_ledger_at(repo, res.final_ref)["claims"]
    assert "x" not in res.surviving_keys
    assert rc1.contested and rc1.contested[0]["pre_existing"] is True and rc1.contested[0]["revert_sha"] is not None
    # blame still shows BOTH the round-0 assertion (verified) and the round-1 contest (unverified)
    bl = dag.blame("x")
    assert any(e["round"] == 0 and e["verified"] is True for e in bl)
    assert any(e["round"] == 1 and e["verified"] is False for e in bl)


# ── 15: an em-dash / newline reason yields an ASCII-safe single-line commit message ──
def test_contested_reason_ascii_safe(tmp_path):
    repo = init_repo(tmp_path)
    dag = ResearchDag(repo, date="20260701", slug="ascii")
    inputs = mk_inputs(
        [mk_claim("s", "is", 1, bid_key="rob"), mk_claim("s", "is", 2, bid_key="con")],
        ["rob"],
        cnv=[{"bid_key": "con", "kind": "refuted", "reasons": ["source — states 1\nnot 2 — unsupported"]}],
    )
    rc = dag.commit_round(0, inputs, seed_ref=head_sha(repo))
    assert_sha = rc.contested[0]["assert_sha"]
    subject = _git(repo, "show", "-s", "--format=%s", assert_sha).stdout
    assert "—" not in subject and "\n" not in subject.strip()   # em-dash folded, single line
    assert subject.startswith("research: contested claim con REVERTED:")
    # and the round still merged (the revert succeeded on an ASCII-safe message)
    assert "con" not in read_ledger_at(repo, rc.merge_sha)["claims"]
