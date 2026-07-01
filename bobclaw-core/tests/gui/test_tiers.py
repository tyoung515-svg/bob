"""Tests for core.gui.tiers (deterministic tier resolver, MS2-G1)."""
from __future__ import annotations

import ast
import pathlib

from core.gui import Action, ActionKind
from core.gui.tiers import (
    Tier,
    classify_gui_action,
    classify_tool,
    is_protected_path,
    requires_human,
    resolve_tier,
    route_action,
)
from core.permissions import Scope, evaluate_action, _ALWAYS_HUMAN_ACTIONS


# ─── 1. every ActionKind classifies ────────────────────────────────────────────
def test_every_actionkind_classifies() -> None:
    for kind in list(ActionKind):
        action = Action(kind)
        tier = classify_gui_action(action)
        assert isinstance(tier, Tier)
        if kind in (ActionKind.NOOP, ActionKind.SCROLL):
            assert tier == Tier.READ_ONLY, f"{kind} should be READ_ONLY"
        else:
            # KEY, TYPE, CLICK
            assert tier == Tier.WRITE_LOCAL, f"{kind} should be WRITE_LOCAL"


# ─── 2. real MCP/tool set tiers ──────────────────────────────────────────────
def test_real_tool_set_tiers() -> None:
    read_only_tools = [
        "get_server_time",
        "list_backends",
        "read_file",
        "mcp__filesystem__read_file",
    ]
    for tool in read_only_tools:
        assert classify_tool(tool) == Tier.READ_ONLY, f"{tool} should be READ_ONLY"

    write_local_tools = [
        "create_project",
        "create_team",
        "cc_edit",
        "chat_with_face",
        "run_council",
    ]
    for tool in write_local_tools:
        assert classify_tool(tool) == Tier.WRITE_LOCAL, f"{tool} should be WRITE_LOCAL"


# ─── 3. floor set → Full-Access ───────────────────────────────────────────────
def test_floor_set_is_full_access() -> None:
    for at in _ALWAYS_HUMAN_ACTIONS:
        assert classify_tool(at) is Tier.FULL_ACCESS, f"{at} must be FULL_ACCESS"


# ─── 4. delete protected-glob → Full-Access ──────────────────────────────────
def test_delete_protected_glob_full_access() -> None:
    paths = [
        {"path": "repo/.git/config"},
        {"path": "C:/proj/.secrets/bobclaw.env"},
        {"path": "id_rsa.pem"},
    ]
    for args in paths:
        assert (
            classify_tool("delete", args) is Tier.FULL_ACCESS
        ), f"delete({args}) should be FULL_ACCESS"


# ─── 5. delete scratch → Write-Local ──────────────────────────────────────────
def test_delete_scratch_write_local() -> None:
    paths = [
        {"path": "scratch/note.txt"},
        {"path": "/tmp/x.log"},
    ]
    for args in paths:
        assert (
            classify_tool("delete", args) is Tier.WRITE_LOCAL
        ), f"delete({args}) should be WRITE_LOCAL"


# ─── 6. delete ambiguous → Full-Access (fail closed) ─────────────────────────
def test_delete_ambiguous_fail_closed() -> None:
    # Non-protected, non-scratch path -> fail closed to FULL_ACCESS
    assert (
        classify_tool("delete", {"path": "data/customers.csv"}) is Tier.FULL_ACCESS
    )
    # Missing path -> fail closed
    assert classify_tool("delete", {}) is Tier.FULL_ACCESS


# ─── 7. pay zero → Social, pay nonzero → Full-Access ──────────────────────────
def test_pay_zero_social_nonzero_full() -> None:
    # amount=0 -> SOCIAL
    assert classify_tool("pay", {"amount": 0}) is Tier.SOCIAL
    assert classify_tool("pay", {"amount": "0.00"}) is Tier.SOCIAL
    assert classify_tool("pay", {"amount": 0.0}) is Tier.SOCIAL

    # amount>0 -> FULL_ACCESS
    assert classify_tool("pay", {"amount": 49.99}) is Tier.FULL_ACCESS
    assert classify_tool("pay", {"amount": "5"}) is Tier.FULL_ACCESS
    # missing amount -> FULL_ACCESS (fail closed)
    assert classify_tool("pay", {}) is Tier.FULL_ACCESS

    # Also test aliases
    for alias in ("pay_invoice", "send_payment", "transfer", "charge"):
        assert classify_tool(alias, {"amount": 0}) is Tier.SOCIAL
        assert classify_tool(alias, {"amount": 100}) is Tier.FULL_ACCESS


# ─── 8. send self → Write-Local, send other → Social ──────────────────────────
def test_send_self_write_local_other_social() -> None:
    # Self -> WRITE_LOCAL
    self_args = [
        {"recipient": "self"},
        {"to": "me"},
        {"recipient": "a@b.com", "from": "a@b.com"},
    ]
    for args in self_args:
        assert (
            classify_tool("send", args) is Tier.WRITE_LOCAL
        ), f"send({args}) should be WRITE_LOCAL"

    # Other -> SOCIAL
    other_args = [
        {"recipient": "boss@co.com"},
        {},  # missing recipient -> SOCIAL (cannot prove self)
    ]
    for args in other_args:
        assert (
            classify_tool("send", args) is Tier.SOCIAL
        ), f"send({args}) should be SOCIAL"


# ─── 9. floor wins over verb rule ─────────────────────────────────────────────
def test_floor_wins_over_verb_rule() -> None:
    # email_send is floor, so always FULL_ACCESS regardless of args
    assert classify_tool("email_send", {"recipient": "self"}) is Tier.FULL_ACCESS
    # purchase is floor, so always FULL_ACCESS even if amount=0
    assert classify_tool("purchase", {"amount": 0}) is Tier.FULL_ACCESS
    # file_delete is floor, so always FULL_ACCESS even with scratch path
    assert classify_tool("file_delete", {"path": "scratch/x"}) is Tier.FULL_ACCESS


# ─── 10. Full-Access routes to human under permissive scope ──────────────────
def test_full_access_routes_human_under_permissive_scope() -> None:
    scope = Scope(
        auto_actions=["delete", "purchase", "email_send"],
        may_touch=["*", "**"],
    )

    # Floor action -> human (even if in auto_actions)
    assert route_action("email_send", scope) == "human"

    # delete with protected path -> human (argument-aware escalation before scope)
    assert (
        route_action("delete", scope, args={"path": "repo/.git/x"}) == "human"
    )

    # requires_human
    assert requires_human(Tier.FULL_ACCESS) is True
    assert requires_human(Tier.WRITE_LOCAL) is False


# ─── 11. route consistency with evaluate_action ──────────────────────────────
def test_route_consistency_with_evaluate_action() -> None:
    # For every floor action, both functions agree on "human"
    for s in (None, Scope(auto_actions=list(_ALWAYS_HUMAN_ACTIONS))):
        for at in _ALWAYS_HUMAN_ACTIONS:
            assert route_action(at, s) == "human", f"route_action({at}, {s}) should be human"
            assert evaluate_action(at, s) == "human", f"evaluate_action({at}, {s}) should be human"

    # Scratch delete with matching scope -> "auto"
    scope2 = Scope(auto_actions=["delete"], may_touch=["scratch/*"])
    assert (
        route_action("delete", scope2, args={"path": "scratch/n.txt"}) == "auto"
    )


# ─── 12. heuristic fallback ─────────────────────────────────────────────────
def test_heuristic_fallback() -> None:
    # Dangerous verbs -> FULL_ACCESS
    for name in ("wipe_database", "send_invoice", "destroy_all"):
        assert classify_tool(name) is Tier.FULL_ACCESS, f"{name} should be FULL_ACCESS"

    # Read verbs -> READ_ONLY
    for name in ("list_widgets", "get_status", "view_profile"):
        assert classify_tool(name) is Tier.READ_ONLY, f"{name} should be READ_ONLY"

    # Neutral -> WRITE_LOCAL
    for name in ("frobnicate", "reindex", "touch"):
        assert classify_tool(name) is Tier.WRITE_LOCAL, f"{name} should be WRITE_LOCAL"


# ─── 13. no model and pure (AST import analysis + string-only path check) ────
def test_no_model_and_pure() -> None:
    # Parse the module's AST and inspect the ACTUAL import targets — a substring
    # scan is wrong here because the module docstring legitimately *names*
    # core.backends/core.nodes to document what it does NOT import.
    module_path = (
        pathlib.Path(__file__).parents[2] / "core" / "gui" / "tiers.py"
    ).resolve()
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_roots = ("core.backends", "core.nodes", "aiohttp", "requests", "httpx", "urllib")
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
    for mod in imported:
        for root in forbidden_roots:
            assert not (mod == root or mod.startswith(root + ".")), (
                f"module must not import {mod} (matches forbidden {root})"
            )

    # No reference anywhere to the backend dispatch entrypoint (defensive: even if
    # it were imported under an alias, the name would appear as a Name/Attribute).
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "_send_to_backend" not in (names | attrs), "module must not call _send_to_backend"
    # No DYNAMIC imports that could smuggle a backend past the static check. (audit r8.)
    assert "importlib" not in imported and "importlib" not in names, "module must not use importlib"
    assert "__import__" not in names, "module must not use __import__"

    # Prove string-only analysis: a NON-EXISTENT protected path still classifies
    # (no filesystem access — pure string inspection).
    assert is_protected_path("nope/.git/x") is True


# ─── 14. resolve_tier dispatch and Tier label/ordering ──────────────────────
def test_resolve_tier_dispatch_and_label() -> None:
    # Action -> classify_gui_action
    assert resolve_tier(Action(ActionKind.SCROLL)) == Tier.READ_ONLY
    # str -> classify_tool
    assert resolve_tier("email_send") == Tier.FULL_ACCESS

    # Labels
    assert Tier.FULL_ACCESS.label == "Full-Access"
    assert Tier.READ_ONLY.label == "Read-Only"

    # Ordering (IntEnum)
    assert Tier.FULL_ACCESS > Tier.SOCIAL > Tier.WRITE_LOCAL > Tier.READ_ONLY


# ─── audit r1 regression tests (real under-protection fixes) ──────────────────

def test_delete_path_traversal_fails_closed() -> None:
    # A scratch-looking path with a `..` traversal can escape the sandbox -> must be Full-Access,
    # NOT downgraded to Write-Local by the scratch segment. (audit r1, focus 0)
    assert classify_tool("delete", {"path": "scratch/../../etc/passwd"}) is Tier.FULL_ACCESS
    assert classify_tool("delete", {"path": "tmp/../secrets/key"}) is Tier.FULL_ACCESS
    assert is_protected_path("scratch/../x") is True


def test_permissive_scope_cannot_deescalate_intrinsic_tier() -> None:
    # A maximally-permissive scope (may_touch=['*']) must NOT make a system/secret path look
    # scratch. delete('/etc/passwd') under such a scope stays Full-Access. (audit r1, focus 1)
    wide = Scope(auto_actions=["delete"], may_touch=["*", "**"])
    assert classify_tool("delete", {"path": "/etc/passwd"}, scope=wide) is Tier.FULL_ACCESS
    assert route_action("delete", wide, args={"path": "/etc/passwd"}) == "human"
    # an absolute system path is protected even with no scope
    assert is_protected_path("/etc/passwd") is True
    assert is_protected_path("/usr/bin/python") is True
    # a relative project dir named like a system root is NOT false-flagged
    assert is_protected_path("var/data.txt") is False
    assert is_protected_path("bin/run.sh") is False


def test_heuristic_catches_obvious_destructive_unknowns() -> None:
    # Unknown destructive verbs must fail closed to Full-Access, not the WRITE_LOCAL default.
    # (audit r1, focus 3)
    for name in ("format_disk", "erase_volume", "truncate_table", "factory_reset",
                 "uninstall_app", "kill_process"):
        assert classify_tool(name) is Tier.FULL_ACCESS, f"{name} should be FULL_ACCESS"


def test_write_to_protected_path_escalates() -> None:
    # A write to a protected path escalates to Full-Access; a benign/local write stays Write-Local;
    # a write with no path stays Write-Local (writes are not fail-closed-to-Full like deletes).
    # (audit r1, focus 3/4)
    assert classify_tool("write_file", {"path": ".ssh/authorized_keys"}) is Tier.FULL_ACCESS
    assert classify_tool("mcp__filesystem__write_file", {"path": "repo/.git/config"}) is Tier.FULL_ACCESS
    assert classify_tool("write_file", {"path": "scratch/note.txt"}) is Tier.WRITE_LOCAL
    assert classify_tool("mcp__filesystem__write_file") is Tier.WRITE_LOCAL
    assert classify_tool("save", {"path": "id_rsa.pem"}) is Tier.FULL_ACCESS


def test_move_copy_destination_keys_escalate() -> None:
    # audit r2: move/copy/rename use dest/destination keys, not `path` — a move to a protected
    # destination must still escalate to Full-Access.
    assert classify_tool("move", {"dest": "/etc/passwd"}) is Tier.FULL_ACCESS
    assert classify_tool("copy", {"destination": ".ssh/authorized_keys"}) is Tier.FULL_ACCESS
    assert classify_tool("rename", {"new_path": "repo/.git/config"}) is Tier.FULL_ACCESS
    # a delete that declares its target via `dest`/`destination` is not missed either
    assert classify_tool("delete", {"dest": "id_rsa.pem"}) is Tier.FULL_ACCESS
    # a move entirely within scratch stays Write-Local
    assert classify_tool("move", {"src": "scratch/a", "dest": "scratch/b"}) is Tier.WRITE_LOCAL


def test_absolute_system_and_windows_paths_protected() -> None:
    # audit r2: macOS /private root + Windows system locations are protected; relative project
    # dirs that merely share a name are NOT false-flagged.
    assert is_protected_path("/private/etc/passwd") is True
    assert is_protected_path("/etc/passwd") is True
    assert is_protected_path("C:/Windows/System32/cmd.exe") is True
    assert is_protected_path("C:\\Windows\\System32\\drivers\\etc\\hosts") is True
    assert is_protected_path("C:/Program Files/app/x.dll") is True
    assert is_protected_path("C:/ProgramData/secret") is True
    # not false-flagged:
    assert is_protected_path("myproject/var/data.txt") is False
    assert is_protected_path("src/etc/helpers.py") is False


def test_whitespace_padded_traversal_and_leading_space_fail_closed() -> None:
    # audit r3: a whitespace-padded `..` segment or a leading-space absolute path must NOT slip past.
    assert classify_tool("delete", {"path": "scratch/.. /etc/passwd"}) is Tier.FULL_ACCESS
    assert classify_tool("delete", {"path": "scratch/  ../secret"}) is Tier.FULL_ACCESS
    assert is_protected_path(" /etc/passwd") is True
    assert is_protected_path("scratch/.. /x") is True


def test_list_valued_path_args_are_flattened() -> None:
    # audit r3: a list/tuple path arg must be flattened so a protected member is not hidden by str().
    assert classify_tool("delete", {"paths": ["/etc/passwd", "scratch/x"]}) is Tier.FULL_ACCESS
    assert classify_tool("write_file", {"files": ("scratch/a", ".ssh/id_rsa")}) is Tier.FULL_ACCESS
    # an all-scratch list delete stays Write-Local
    assert classify_tool("delete", {"paths": ["scratch/a", "tmp/b"]}) is Tier.WRITE_LOCAL


def test_edit_verbs_are_argument_aware() -> None:
    # audit r3: file-editing verbs escalate on a protected path, base Write-Local otherwise.
    assert classify_tool("edit_file", {"path": ".ssh/authorized_keys"}) is Tier.FULL_ACCESS
    assert classify_tool("patch", {"path": "repo/.git/config"}) is Tier.FULL_ACCESS
    assert classify_tool("edit_file", {"path": "scratch/notes.md"}) is Tier.WRITE_LOCAL


def test_heuristic_whole_token_no_false_positives() -> None:
    # audit r3: whole-token matching fixes substring noise in BOTH directions.
    # "amount" must NOT trip on the "mount" dangerous token:
    assert classify_tool("get_amount") is Tier.READ_ONLY
    # "confirm"/"format_string" must NOT trip on the "rm" token via substring:
    assert classify_tool("confirm_dialog") is Tier.WRITE_LOCAL
    # but genuinely privileged unknown sysadmin verbs DO escalate (whole-token):
    for name in ("shutdown_now", "reboot_host", "chmod_file", "mount_volume",
                 "umount_drive", "systemctl_stop", "kill_job"):
        assert classify_tool(name) is Tier.FULL_ACCESS, f"{name} should be FULL_ACCESS"


def test_pay_bool_amount_is_not_zero() -> None:
    # audit r3: a bool amount must never de-escalate a payment (bool is an int subclass).
    assert classify_tool("pay", {"amount": False}) is Tier.FULL_ACCESS
    assert classify_tool("pay", {"amount": True}) is Tier.FULL_ACCESS


def test_verb_spelling_variants_are_argument_aware() -> None:
    # audit r4: spelling variants route to the delete/write group by whole-token, so a protected
    # path still escalates (not only the exact alias names).
    assert classify_tool("move_file", {"dest": "/etc/passwd"}) is Tier.FULL_ACCESS
    assert classify_tool("copy_file", {"destination": ".ssh/id_rsa"}) is Tier.FULL_ACCESS
    assert classify_tool("unlink_file", {"path": "repo/.git/config"}) is Tier.FULL_ACCESS
    assert classify_tool("del_file", {"path": "id_rsa.pem"}) is Tier.FULL_ACCESS
    assert classify_tool("save_as", {"path": ".aws/credentials"}) is Tier.FULL_ACCESS
    # a scratch-only variant stays Write-Local
    assert classify_tool("move_file", {"src": "scratch/a", "dest": "scratch/b"}) is Tier.WRITE_LOCAL


def test_path_normalization_double_slash_and_dot_segments() -> None:
    # audit r4: "//etc/passwd" and "/etc/./passwd" must not dodge the system-prefix check.
    assert is_protected_path("//etc/passwd") is True
    assert is_protected_path("/etc/./passwd") is True
    assert classify_tool("delete", {"path": "//etc/passwd"}) is Tier.FULL_ACCESS
    # macOS system-wide locations
    assert is_protected_path("/Library/Preferences/x.plist") is True
    assert is_protected_path("/System/Library/y") is True


def test_case_insensitive_floor_and_verbs() -> None:
    # audit r5: a case variant must NOT evade the floor / verb groups / heuristic.
    assert classify_tool("EMAIL_SEND") is Tier.FULL_ACCESS
    assert classify_tool("Email_Send") is Tier.FULL_ACCESS
    assert classify_tool("FILE_DELETE") is Tier.FULL_ACCESS
    assert route_action("EMAIL_SEND", Scope(auto_actions=["email_send"])) == "human"
    # all-caps generic verbs still classify via the case-robust tokenizer
    assert classify_tool("DELETE", {"path": "repo/.git/x"}) is Tier.FULL_ACCESS
    assert classify_tool("Pay", {"amount": 0}) is Tier.SOCIAL
    assert classify_tool("GET_SERVER_TIME") is Tier.READ_ONLY  # type-map, case-folded


def test_create_and_exec_verbs() -> None:
    # audit r5: file-creation verbs are argument-aware; code/command-exec verbs fail closed.
    assert classify_tool("create_file", {"path": "/etc/cron.d/evil"}) is Tier.FULL_ACCESS
    assert classify_tool("create_file", {"path": "scratch/x"}) is Tier.WRITE_LOCAL
    assert classify_tool("execute_command") is Tier.FULL_ACCESS
    assert classify_tool("eval_expr") is Tier.FULL_ACCESS
    assert classify_tool("run_script") is Tier.FULL_ACCESS


def test_route_gate_for_uncovered_action_under_scope() -> None:
    # audit r5: a non-Full action not in auto_actions must route to "gate" (critic), never "auto".
    scope = Scope(auto_actions=["get_server_time"], may_touch=["scratch/*"])
    assert route_action("create_project", scope) == "gate"
    assert route_action("get_server_time", scope) == "auto"


def test_is_protected_path_scope_may_not_touch_escalates() -> None:
    # audit r5: a scope's may_not_touch makes an otherwise-unprotected path protected (escalation).
    scope = Scope(may_not_touch=["config/*.toml"])
    assert is_protected_path("config/prod.toml", scope) is True
    assert is_protected_path("config/prod.toml") is False  # no scope -> not protected


def test_send_and_write_aliases_covered() -> None:
    # audit r5: exercise alias members beyond the canonical verb.
    assert classify_tool("send_message", {"recipient": "boss@co.com"}) is Tier.SOCIAL
    assert classify_tool("send_dm", {"recipient": "self"}) is Tier.WRITE_LOCAL
    assert classify_tool("overwrite", {"path": "id_rsa.pem"}) is Tier.FULL_ACCESS
    assert classify_tool("upload", {"path": "scratch/x"}) is Tier.WRITE_LOCAL


def test_symlink_to_protected_target_escalates() -> None:
    # audit r6: a symlink/link to a protected target is a real attack vector.
    assert classify_tool("symlink", {"target": ".ssh/authorized_keys"}) is Tier.FULL_ACCESS
    assert classify_tool("link", {"path": "x", "target": "/etc/passwd"}) is Tier.FULL_ACCESS
    assert classify_tool("create_symlink", {"target": "repo/.git/config"}) is Tier.FULL_ACCESS
    assert classify_tool("symlink", {"target": "scratch/a", "path": "scratch/b"}) is Tier.WRITE_LOCAL


def test_route_action_no_scope_non_floor_is_human() -> None:
    # audit r6: with no scope, a non-Full action fails closed to human (delegated to evaluate_action).
    assert route_action("frobnicate") == "human"
    assert route_action("get_server_time") == "human"  # no scope -> human even for read-only


def test_heuristic_precedence_dangerous_over_read() -> None:
    # audit r6: an ambiguous name with BOTH a dangerous and a read token resolves dangerous-first.
    assert classify_tool("delete_status") is Tier.FULL_ACCESS
    assert classify_tool("list_and_purge") is Tier.FULL_ACCESS


def test_credential_dirs_and_whitespace_path_protected() -> None:
    # audit r7: credential/cluster-secret dirs are protected; a whitespace-only path fails closed.
    assert classify_tool("write_file", {"path": "home/u/.gnupg/private-keys-v1.d/x"}) is Tier.FULL_ACCESS
    assert classify_tool("delete", {"path": ".kube/config"}) is Tier.FULL_ACCESS
    assert is_protected_path("~/.docker/config.json") is True
    assert is_protected_path("project/.vault/token") is True
    # whitespace-only path -> protected (fail closed)
    assert is_protected_path("   ") is True
    assert classify_tool("write_file", {"path": "   "}) is Tier.FULL_ACCESS
    # /System/Library is already covered by the /system/ prefix (regression guard)
    assert is_protected_path("/System/Library") is True


def test_pay_tiny_underflow_amount_is_not_zero() -> None:
    # audit r8: float("1e-1000") underflows to 0.0; Decimal must keep a tiny non-zero payment FULL.
    assert classify_tool("pay", {"amount": "1e-1000"}) is Tier.FULL_ACCESS
    assert classify_tool("pay", {"amount": "0.0000000001"}) is Tier.FULL_ACCESS
    # genuine zeros still de-escalate
    assert classify_tool("pay", {"amount": "0"}) is Tier.SOCIAL
    assert classify_tool("pay", {"amount": " 0 "}) is Tier.SOCIAL


def test_send_with_protected_path_escalates() -> None:
    # audit r8: a send/post carrying a protected file path escalates over the self/social rule.
    assert classify_tool("post", {"recipient": "self", "path": ".ssh/authorized_keys"}) is Tier.FULL_ACCESS
    assert classify_tool("send", {"recipient": "self"}) is Tier.WRITE_LOCAL  # unchanged when benign


def test_copy_from_source_and_cloud_cred_dirs_and_cert_suffixes() -> None:
    # audit r8: copy "from" a protected source escalates; cloud cred dirs + cert suffixes protected.
    assert classify_tool("copy", {"from": "/etc/passwd", "to": "scratch/x"}) is Tier.FULL_ACCESS
    assert classify_tool("write_file", {"path": "u/.azure/credentials"}) is Tier.FULL_ACCESS
    assert is_protected_path("home/.gcloud/creds.json") is True
    assert is_protected_path("certs/server.p12") is True
    assert is_protected_path("ca/root.crt") is True
    # common write abbreviations are argument-aware
    assert classify_tool("mv", {"dest": "/etc/hosts"}) is Tier.FULL_ACCESS
    assert classify_tool("store", {"path": "id_ed25519.key"}) is Tier.FULL_ACCESS


def test_filesystem_root_is_protected() -> None:
    # audit r9: writing/deleting at the filesystem or drive root is catastrophic -> always protected.
    assert is_protected_path("/") is True
    assert is_protected_path("C:/") is True
    assert is_protected_path("C:\\") is True
    assert is_protected_path("D:") is True
    assert classify_tool("write_file", {"path": "/"}) is Tier.FULL_ACCESS
    assert classify_tool("delete", {"path": "C:/"}) is Tier.FULL_ACCESS
    # a normal absolute file is NOT the root (regression guard)
    assert is_protected_path("/home/user/notes.txt") is False
    assert classify_tool("nuke_database") is Tier.FULL_ACCESS
    assert classify_tool("trunc_table") is Tier.FULL_ACCESS


def test_more_path_keys_and_payment_alias() -> None:
    # audit r10: common path-argument keys are extracted; bare "payment" hits the pay rule.
    assert classify_tool("write_file", {"file_path": "/etc/passwd"}) is Tier.FULL_ACCESS
    assert classify_tool("delete", {"directory": "repo/.git"}) is Tier.FULL_ACCESS
    # the "input" key is extracted for a path-checking (write/delete) verb
    assert classify_tool("write_file", {"input": ".ssh/id_rsa"}) is Tier.FULL_ACCESS
    assert classify_tool("write_file", {"folder": "scratch", "file": "x"}) is Tier.WRITE_LOCAL
    # bare "payment": nonzero -> Full-Access, zero -> Social (not the WRITE_LOCAL heuristic default)
    assert classify_tool("payment", {"amount": 50}) is Tier.FULL_ACCESS
    assert classify_tool("payment", {"amount": 0}) is Tier.SOCIAL
    assert classify_tool("remit", {"amount": 100}) is Tier.FULL_ACCESS
    assert classify_tool("write_file", {"file_name": ".ssh/authorized_keys"}) is Tier.FULL_ACCESS
    assert classify_tool("delete", {"location": "/etc/passwd"}) is Tier.FULL_ACCESS


def test_all_write_and_send_aliases_behave() -> None:
    # audit r11: pin EVERY write alias (protected path -> Full; benign -> Write-Local) and EVERY send
    # alias (other -> Social, self -> Write-Local) so a future refactor can't silently drop one.
    for w in ("append", "insert", "replace", "overwrite", "save", "upload", "put", "mkdir",
              "modify", "edit", "store", "persist", "dump", "mv", "cp"):
        assert classify_tool(w, {"path": "id_rsa.pem"}) is Tier.FULL_ACCESS, f"{w} protected"
        assert classify_tool(w, {"path": "scratch/x"}) is Tier.WRITE_LOCAL, f"{w} benign"
    for s in ("send_message", "send_dm", "dm", "message", "post", "broadcast", "notify",
              "mention", "reply", "comment", "announce", "publish"):
        assert classify_tool(s, {"recipient": "boss@co.com"}) is Tier.SOCIAL, f"{s} other"
        assert classify_tool(s, {"recipient": "self"}) is Tier.WRITE_LOCAL, f"{s} self"


def test_send_nonself_with_protected_path_escalates() -> None:
    # audit r11: the protected-path check escalates a send BEFORE the self/other branch.
    assert classify_tool("post", {"recipient": "boss@co.com", "path": ".ssh/id_rsa"}) is Tier.FULL_ACCESS
    assert classify_tool("send_message", {"recipient": "x", "file": "/etc/passwd"}) is Tier.FULL_ACCESS


def test_format_string_no_rm_substring_trap() -> None:
    # audit r11: whole-token matching means "format_string" does NOT trip the "rm" token.
    assert classify_tool("format_string") is Tier.FULL_ACCESS  # "format" IS a dangerous token
    assert classify_tool("transform_data") is Tier.WRITE_LOCAL  # "rm" not a token here; neutral
    assert classify_tool("reaffirm_choice") is Tier.WRITE_LOCAL
