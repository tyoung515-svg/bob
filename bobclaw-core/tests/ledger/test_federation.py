"""MS-1 (Ledger 3b) — federation registry tests. Uses the shared `git_repo` fixture
(tests/ledger/conftest.py) + `tmp_path` for the registry file. Cross-checks the resolver's bound
projection methods against `core.ledger.project` directly.

NOTE (manager): the worker-staged version had wrong `pytest.raises(match=...)` regexes (didn't match
the impl's actual FederationError text) and captured `base = "HEAD"` (a string) before the commit so
the diff/key comparisons were against a moving ref — both fixed here (base captured as a SHA pre-commit;
match strings aligned to the impl). These were test-arrangement bugs, not impl bugs.
"""
import pytest
import json
import subprocess
from pathlib import Path

from core.ledger.federation import (
    FederationRegistry,
    FederationError,
    ResolvedInstance,
    default_registry_path,
    REGISTRY_VERSION,
    DEFAULT_LEDGER_DIR,
)
from core.ledger import project


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _git(repo: str, *args: str) -> None:
    """Run a git command in *repo*, raise on failure."""
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _git_output(repo: str, *args: str) -> str:
    """Run a git command and return stdout (stripped)."""
    proc = subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _write_claim(repo: str, claim_id: str, statement: str) -> None:
    """Write a claim file under ledger/claims/."""
    claim_dir = Path(repo) / "ledger" / "claims"
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / f"{claim_id}.json").write_text(json.dumps({"id": claim_id, "statement": statement}))


def _append_event(repo: str, event: dict) -> None:
    """Append a JSON event line to ledger/events.jsonl."""
    events_file = Path(repo) / "ledger" / "events.jsonl"
    with events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


# --------------------------------------------------------------------------- #
# CRUD + round-trip
# --------------------------------------------------------------------------- #

def test_crud_roundtrip(tmp_path: Path) -> None:
    reg_path = tmp_path / "registry.json"
    reg = FederationRegistry(reg_path)
    inst1 = reg.register("alpha", "/tmp/alpha", collection="col_a", dim=128, meta={"version": 1})
    inst2 = reg.register("beta", "/tmp/beta", collection="col_b", dim=256, ledger_dir="my_ledger")
    inst3 = reg.register("gamma", "/tmp/gamma", collection="col_c", dim=512,
                         meta={"note": "test", "nested": {"k": 1}})
    assert inst1["name"] == "alpha"
    assert inst2["name"] == "beta"
    assert inst3["meta"]["nested"]["k"] == 1
    reg.save()

    # the persisted file carries the version + an instances dict keyed by name (no embedded "name")
    on_disk = json.loads(reg_path.read_text(encoding="utf-8"))
    assert on_disk["version"] == REGISTRY_VERSION
    assert set(on_disk["instances"]) == {"alpha", "beta", "gamma"}
    assert "name" not in on_disk["instances"]["alpha"]

    # fresh registry load
    reg2 = FederationRegistry(reg_path).load()
    assert reg2.names() == ["alpha", "beta", "gamma"]
    a = reg2.get("alpha")
    assert a["repo"] == "/tmp/alpha"
    assert a["collection"] == "col_a"
    assert a["dim"] == 128
    assert isinstance(a["dim"], int)
    assert a["ledger_dir"] == DEFAULT_LEDGER_DIR
    assert a["meta"] == {"version": 1}

    b = reg2.get("beta")
    assert b["dim"] == 256
    assert b["ledger_dir"] == "my_ledger"
    assert b["meta"] == {}

    g = reg2.get("gamma")
    assert g["dim"] == 512
    assert g["meta"] == {"note": "test", "nested": {"k": 1}}

    lst = reg2.list()
    assert len(lst) == 3
    assert [x["name"] for x in lst] == ["alpha", "beta", "gamma"]


# --------------------------------------------------------------------------- #
# duplicates
# --------------------------------------------------------------------------- #

def test_dup_name_rejected(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("x", "/r1", collection="c1", dim=64)
    with pytest.raises(FederationError, match="already registered"):
        reg.register("x", "/r2", collection="c2", dim=64)
    # overwrite works
    reg.register("x", "/r3", collection="c3", dim=128, overwrite=True)
    assert reg.get("x")["repo"] == "/r3"


def test_dup_collection_rejected(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("a", "/p1", collection="col", dim=64)
    with pytest.raises(FederationError, match="already assigned"):
        reg.register("b", "/p2", collection="col", dim=64)


def test_overwrite_to_other_instance_collection_rejected(tmp_path: Path) -> None:
    """overwrite=True must still enforce collection uniqueness against OTHER instances.

    Without this test, a bug that skipped collection-uniqueness on overwrite would
    silently break the collection->repo invariant while all existing tests passed.
    """
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("x", "/r1", collection="c1", dim=64)
    reg.register("y", "/r2", collection="c2", dim=64)
    with pytest.raises(FederationError, match="already assigned"):
        reg.register("x", "/r3", collection="c2", dim=64, overwrite=True)
    # sanity: overwriting to a still-unique collection is fine
    reg.register("x", "/r3", collection="c3", dim=128, overwrite=True)
    assert reg.get("x")["collection"] == "c3"


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name, repo, collection, dim, ledger_dir",
    [
        ("x", "", "c", 1, "ledger"),       # empty repo
        ("x", "/p", "", 1, "ledger"),      # empty collection
        ("x", "/p", "c", 0, "ledger"),     # dim == 0
        ("x", "/p", "c", -1, "ledger"),    # dim negative
        ("x", "/p", "c", True, "ledger"),  # dim bool
        ("x", "/p", "c", 1.5, "ledger"),   # dim float
        ("x", "/p", "c", 1, ""),           # empty ledger_dir
    ],
)
def test_validation_register(tmp_path, name, repo, collection, dim, ledger_dir) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    with pytest.raises(FederationError):
        reg.register(name, repo, collection=collection, dim=dim, ledger_dir=ledger_dir)


def test_register_empty_name_rejected(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    with pytest.raises(FederationError):
        reg.register("", "/p", collection="c", dim=1)


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #

def test_update(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("x", "/p1", collection="c1", dim=64, meta={"k": 1})
    updated = reg.update("x", dim=128, repo="/p2", meta={"k": 2})
    assert updated["dim"] == 128
    assert updated["repo"] == "/p2"
    assert updated["meta"] == {"k": 2}
    assert updated["collection"] == "c1"  # untouched field preserved
    # collection uniqueness enforced on update
    reg.register("y", "/p3", collection="c2", dim=64)
    with pytest.raises(FederationError, match="already assigned"):
        reg.update("x", collection="c2")
    # unknown name
    with pytest.raises(FederationError, match="Unknown instance"):
        reg.update("nobody", dim=10)


def test_update_revalidates(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("x", "/p1", collection="c1", dim=64)
    with pytest.raises(FederationError):
        reg.update("x", dim=0)  # merged record must still validate


# --------------------------------------------------------------------------- #
# unregister
# --------------------------------------------------------------------------- #

def test_unregister(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("a", "/p1", collection="c1", dim=64)
    reg.unregister("a")
    assert reg.names() == []
    with pytest.raises(FederationError, match="Unknown instance"):
        reg.unregister("a")
    with pytest.raises(FederationError, match="Unknown instance"):
        reg.unregister("noexist")


# --------------------------------------------------------------------------- #
# by_collection
# --------------------------------------------------------------------------- #

def test_by_collection(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("first", "/r1", collection="col1", dim=128)
    reg.register("second", "/r2", collection="col2", dim=256)
    r = reg.by_collection("col1")
    assert r["name"] == "first"
    assert r["repo"] == "/r1"
    with pytest.raises(FederationError, match="not found"):
        reg.by_collection("col_unknown")


# --------------------------------------------------------------------------- #
# load tolerance
# --------------------------------------------------------------------------- #

def test_load_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent.json"
    reg = FederationRegistry(p).load()
    assert reg.names() == []


def test_load_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    # top-level list
    p.write_text("[]")
    with pytest.raises(FederationError):
        FederationRegistry(p).load()
    # "instances" not a dict
    p.write_text(json.dumps({"instances": "string"}))
    with pytest.raises(FederationError):
        FederationRegistry(p).load()
    # invalid JSON
    p.write_text("{invalid")
    with pytest.raises(FederationError):
        FederationRegistry(p).load()
    # a record that fails validation
    p.write_text(json.dumps({"instances": {"x": {"repo": "/p", "collection": "c", "dim": 0,
                                                  "ledger_dir": "ledger"}}}))
    with pytest.raises(FederationError):
        FederationRegistry(p).load()


def test_load_rejects_duplicate_collection(tmp_path: Path) -> None:
    """load() enforces collection-uniqueness across records (matching register/update), so a
    hand-edited file can't break the collection->repo map. (audit r2 finding F1)"""
    p = tmp_path / "dup.json"
    p.write_text(json.dumps({"version": 1, "instances": {
        "a": {"repo": "/p1", "collection": "shared", "dim": 1, "ledger_dir": "ledger"},
        "b": {"repo": "/p2", "collection": "shared", "dim": 1, "ledger_dir": "ledger"},
    }}))
    with pytest.raises(FederationError, match="collection 'shared'"):
        FederationRegistry(p).load()


def test_save_failure_is_atomic_no_orphan_tmp(tmp_path: Path) -> None:
    """A non-JSON-serializable meta makes save() raise, but (os.replace is last) the real registry
    file is NEVER corrupted and no orphan .tmp file is left behind. (audit r3 finding 3b — atomicity)"""
    reg_path = tmp_path / "r.json"
    reg = FederationRegistry(reg_path)
    reg.register("ok", "/p", collection="c_ok", dim=1)
    reg.save()
    good = reg_path.read_text(encoding="utf-8")

    # inject a non-serializable meta (a bare object) and attempt to persist -> must raise
    reg.register("bad", "/p2", collection="c_bad", dim=1, meta={"when": object()})
    with pytest.raises(TypeError):
        reg.save()

    # the real file is untouched; the tmp sibling did not survive
    assert reg_path.read_text(encoding="utf-8") == good
    tmp_sibling = reg_path.with_suffix(".tmp" + reg_path.suffix)
    assert not tmp_sibling.exists()


# --------------------------------------------------------------------------- #
# default_registry_path
# --------------------------------------------------------------------------- #

def test_default_registry_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Path-equality (not str) so the assertion is OS-separator agnostic on Windows.
    monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", "/custom/path/instances.json")
    assert default_registry_path() == Path("/custom/path/instances.json")
    monkeypatch.delenv("BOBCLAW_LEDGER_INSTANCES", raising=False)
    p = default_registry_path()
    assert p.name == "ledger_instances.json"
    assert "data" in p.parts


# --------------------------------------------------------------------------- #
# resolve + projection wiring (the core payoff)
# --------------------------------------------------------------------------- #

def test_resolve_projection_wiring(git_repo: str, tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "reg.json")
    reg.register("test_inst", git_repo, collection="test_col", dim=256,
                 ledger_dir="ledger", meta={"env": "test"})
    resolved = reg.resolve("test_inst")
    assert isinstance(resolved, ResolvedInstance)
    assert resolved.name == "test_inst"
    assert resolved.repo == git_repo
    assert resolved.collection == "test_col"
    assert resolved.dim == 256
    assert resolved.meta == {"env": "test"}

    # the resolver's bound methods match calling project.* directly with the same repo+ledger_dir
    assert resolved.read_ledger_at("HEAD") == project.read_ledger_at(git_repo)
    assert resolved.projection_key("HEAD") == project.projection_key(git_repo)

    # capture base as a SHA *before* mutating (a literal "HEAD" would move with the new commit)
    base = _git_output(git_repo, "rev-parse", "HEAD")
    key_before = resolved.projection_key(base)

    _write_claim(git_repo, "Cx", "test claim")
    _append_event(git_repo, {"id": "Ex01"})
    _git(git_repo, "add", "ledger/claims/Cx.json", "ledger/events.jsonl")
    _git(git_repo, "commit", "-m", "add Cx and event")
    head = _git_output(git_repo, "rev-parse", "HEAD")

    diff = resolved.diff_ledger(base, head)
    assert "Cx" in diff["claims_changed"]
    assert diff["events_changed"] is True
    assert "ledger/claims/Cx.json" in diff["added"]
    # projection_key changed once the ledger truth changed
    assert resolved.projection_key(head) != key_before
    # and the resolver delegates faithfully to project.diff_ledger
    assert diff == project.diff_ledger(git_repo, base, head)


def test_resolve_unknown(tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "r.json")
    with pytest.raises(FederationError, match="Unknown instance"):
        reg.resolve("nobody")


# --------------------------------------------------------------------------- #
# is_git
# --------------------------------------------------------------------------- #

def test_is_git(git_repo: str, tmp_path: Path) -> None:
    reg = FederationRegistry(tmp_path / "reg.json")

    reg.register("git_inst", git_repo, collection="c1", dim=64)
    assert reg.resolve("git_inst").is_git() is True

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    reg.register("plain_inst", str(plain_dir), collection="c2", dim=64)
    assert reg.resolve("plain_inst").is_git() is False

    non_exist = tmp_path / "no_such_dir"
    reg.register("missing_inst", str(non_exist), collection="c3", dim=64)
    assert reg.resolve("missing_inst").is_git() is False  # never raises


# --------------------------------------------------------------------------- #
# audit r1 regressions
# --------------------------------------------------------------------------- #

def test_returned_dicts_are_isolated(tmp_path: Path) -> None:
    """get()/by_collection() return DEEP copies; a caller mutating the result (or the meta they
    passed to register) cannot corrupt internal state. (audit r1 finding A — shallow-copy leak)"""
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("x", "/p", collection="c", dim=1, meta={"k": 1})
    got = reg.get("x")
    got["meta"]["k"] = 999
    got["repo"] = "/hacked"
    assert reg.get("x")["meta"]["k"] == 1
    assert reg.get("x")["repo"] == "/p"
    # by_collection result is isolated too
    bc = reg.by_collection("c")
    bc["meta"]["k"] = -1
    assert reg.by_collection("c")["meta"]["k"] == 1
    # the caller's ORIGINAL meta dict can't reach into stored state after register
    orig = {"k": 2}
    reg.register("y", "/p2", collection="c2", dim=1, meta=orig)
    orig["k"] = 888
    assert reg.get("y")["meta"]["k"] == 2


def test_update_meta_none_normalized(tmp_path: Path) -> None:
    """update(meta=None) normalizes to {} (matching register). (audit r1 finding A)"""
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("x", "/p", collection="c", dim=1, meta={"k": 1})
    reg.update("x", meta=None)
    assert reg.get("x")["meta"] == {}
    assert reg.resolve("x").meta == {}


def test_resolve_meta_missing_or_null(tmp_path: Path) -> None:
    """A loaded record with NO meta key or meta==null resolves to meta=={} (a dict). (audit r1 A/E)"""
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"version": 1, "instances": {
        "a": {"repo": "/p", "collection": "ca", "dim": 1, "ledger_dir": "ledger"},          # no meta
        "b": {"repo": "/p2", "collection": "cb", "dim": 1, "ledger_dir": "ledger", "meta": None},
    }}))
    reg = FederationRegistry(p).load()
    assert reg.resolve("a").meta == {}
    assert reg.resolve("b").meta == {}


def test_resolver_threads_ledger_dir(git_repo: str, tmp_path: Path) -> None:
    """The resolver passes the instance's ledger_dir through to project.* — a hardcoded 'ledger'
    would make a non-default ledger_dir read identical content. (audit r1 finding D — wiring test gap)
    The git_repo fixture has its ledger under 'ledger/'; an instance pointed at 'not_ledger' must
    read EMPTY truth and a DIFFERENT projection key."""
    reg = FederationRegistry(tmp_path / "r.json")
    reg.register("default", git_repo, collection="cd", dim=1, ledger_dir="ledger")
    reg.register("custom", git_repo, collection="cc", dim=1, ledger_dir="not_ledger")
    d = reg.resolve("default")
    c = reg.resolve("custom")
    assert len(d.read_ledger_at("HEAD")["events"]) == 1   # the seed event under ledger/
    assert c.read_ledger_at("HEAD")["events"] == []        # nothing under not_ledger/
    assert c.read_ledger_at("HEAD")["claims"] == {}
    assert d.projection_key("HEAD") != c.projection_key("HEAD")


@pytest.mark.parametrize("field, bad", [
    ("repo", 1), ("collection", 1), ("ledger_dir", 1), ("meta", "not-a-dict"),
])
def test_validation_register_nonstring_types(tmp_path: Path, field: str, bad) -> None:
    """Non-string repo/collection/ledger_dir (and a non-dict meta) raise FederationError, NOT a raw
    AttributeError — the isinstance() guard short-circuits before .strip(). (audit r1 finding B —
    confirms the guard; impl already correct)"""
    kwargs = dict(collection="c", dim=1, ledger_dir="ledger", meta={})
    repo = "/p"
    if field == "repo":
        repo = bad
    else:
        kwargs[field] = bad
    reg = FederationRegistry(tmp_path / "r.json")
    with pytest.raises(FederationError):
        reg.register("x", repo, **kwargs)
