"""
core/gui/tiers.py — MS2-G1 deterministic action-classification tier resolver

Maps every GUI *Action* (by its ActionKind) and every action-type / MCP-tool name (+ call args) to a
four-tier classification (Read-Only / Write-Local / Social / Full-Access). The argument‑aware rules
(delete‑path, pay‑amount, send‑recipient) close the under‑protection holes of a bare type‑to‑tier map.
Full‑Access ⇒ mechanical human interrupt regardless of model intent or scope.

Purity guarantee:  No model calls, no I/O, no imports from core.backends / core.nodes / any HTTP
module.  Entirely deterministic; target latency ≤10 ms per call (DECISIONS-MS2 D1/OD3).

Imports only:  stdlib (``enum``), ``collections.abc.Mapping``, ``core.permissions``
(``evaluate_action``, ``evaluate_path``, ``Scope``, ``_ALWAYS_HUMAN_ACTIONS``) and ``core.gui.types``
(``Action``, ``ActionKind``).  ``core/permissions.py`` is composed read‑only; this module is purely
additive.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import IntEnum

import core.permissions as permissions
from core.gui.types import Action, ActionKind

# ─── Tier enum ─────────────────────────────────────────────────────────────────

class Tier(IntEnum):
    """Four‑tier classification for GUIs actions and tools.  Severity increases with value."""

    READ_ONLY = 0      # pure observation: nothing mutates
    WRITE_LOCAL = 1    # reversible local/sandboxed state change
    SOCIAL = 2         # outward‑facing / affects other people
    FULL_ACCESS = 3    # irreversible / high‑blast‑radius

    @property
    def label(self) -> str:
        """Return the canonical hyphenated label for this tier."""
        return {
            Tier.READ_ONLY: "Read-Only",
            Tier.WRITE_LOCAL: "Write-Local",
            Tier.SOCIAL: "Social",
            Tier.FULL_ACCESS: "Full-Access",
        }[self]


ALWAYS_HUMAN_TIER: Tier = Tier.FULL_ACCESS
"""Module constant: the tier that always requires human approval."""

# ─── Internal segment sets (pure string patterns) ──────────────────────────────

_PROTECTED_SEGMENTS: frozenset[str] = frozenset(
    {
        ".git", ".ssh", ".secrets", ".aws", "secrets", "node_modules", "system32", "windows",
        # Windows system locations (single segments after split on "/"); low false-positive risk.
        "programdata", "program files", "program files (x86)",
        # audit r7: high-value credential / cluster-secret directories (fail-closed denylist).
        ".gnupg", ".gpg", ".pgp", ".kube", ".docker", ".vault", ".keychain", ".credentials",
        ".netrc", ".npmrc", ".pypirc",
        # audit r8: cloud-provider credential directories (siblings of .aws).
        ".azure", ".gcloud",
    }
)

_PROTECTED_SUFFIXES: frozenset[str] = frozenset(
    {".pem", ".key", ".env", ".secret", ".pfx",
     # audit r8: cert / keystore / private-key file extensions.
     ".p12", ".jks", ".cer", ".crt", ".der", ".keystore", ".p8", ".asc"}
)

# Absolute UNIX system roots. Checked as a path PREFIX (not as a bare segment) so a relative
# project dir like ``bin/`` or ``var/`` is NOT false-flagged — only a genuinely absolute system
# path (``/etc/...``) escalates. (audit r1: under-protection of absolute system paths for writes.)
_SYSTEM_PREFIXES: tuple[str, ...] = (
    "/etc/", "/bin/", "/sbin/", "/boot/", "/usr/", "/lib/", "/lib64/", "/root/",
    "/sys/", "/proc/", "/dev/", "/var/", "/opt/",
    "/private/",  # macOS real root for /etc, /var, etc. (audit r2)
    "/library/", "/system/",  # macOS system-wide locations (audit r4)
)
_SYSTEM_ROOTS: frozenset[str] = frozenset(
    {"/etc", "/bin", "/sbin", "/boot", "/usr", "/lib", "/lib64", "/root",
     "/sys", "/proc", "/dev", "/var", "/opt", "/private", "/library", "/system"}
)

# Argument keys that may carry a filesystem path on a delete/write/move/copy/rename call. Broad on
# purpose: extracting MORE candidate paths can only ESCALATE (a protected match -> Full-Access) or
# REFINE a delete (a real scratch path -> Write-Local); it can never under-protect. (audit r2: a
# move/copy with `dest`/`destination` was missed.)
_PATH_KEYS: tuple[str, ...] = (
    "path", "paths", "target", "targets", "file", "files", "filename", "filepath", "filepaths",
    "dest", "destination", "destinations", "dst", "new_path", "newpath", "output",
    "to", "src", "source", "sources", "from",  # "from" = copy/move source (audit r8)
    # audit r10/r11: more common path-argument keys.
    "file_path", "file_name", "filepath", "pathname", "directory", "dir", "folder",
    "input", "infile", "outfile", "in_path", "out_path", "location",
)


def _extract_paths(args: Mapping[str, object]) -> list[str]:
    """Return every non-empty path-like value from *args* as a string (pure, no I/O).

    A list/tuple value (e.g. ``{"paths": ["/etc/passwd", "scratch/x"]}``) is FLATTENED to its
    elements — str()'ing the whole container would hide a protected member from
    :func:`is_protected_path`. (audit r3: list-valued path args.)
    """
    out: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            out.extend(str(v) for v in value if v)
        elif isinstance(value, str):
            out.append(value)
        else:
            out.append(str(value))
    return out

_SCRATCH_SEGMENTS: frozenset[str] = frozenset(
    {"scratch", "tmp", "temp", "_workspace", ".cache"}
)

# ─── Verb alias sets ───────────────────────────────────────────────────────────

_DELETE_ALIASES: frozenset[str] = frozenset(
    {"delete", "remove", "rm", "del", "unlink", "drop"}
)

_PAY_ALIASES: frozenset[str] = frozenset(
    {"pay", "pay_invoice", "send_payment", "transfer", "charge",
     # audit r10: a bare "payment"/"remit" must hit the pay rule (else a nonzero payment would fall
     # to the heuristic as Write-Local instead of Full-Access).
     "payment", "remit", "wire", "disburse"}
)

_SEND_ALIASES: frozenset[str] = frozenset(
    {"send", "send_message", "send_dm", "dm", "message", "post",
     # audit r8: outward-facing social verbs (a broadcast/notify to others is Social, not Write-Local).
     "broadcast", "notify", "mention", "reply", "comment", "announce", "publish"}
)

# Write group: argument-aware like delete, but writes ESCALATE only (protected path -> Full-Access)
# and otherwise stay Write-Local (a write is intrinsically a local mutation; a missing path is NOT
# fail-closed-to-Full the way an ambiguous delete is). Closes the audit-r1 hole where
# ``write_file(.ssh/authorized_keys)`` stayed Write-Local. (Both decorrelated reviewers flagged it.)
_WRITE_ALIASES: frozenset[str] = frozenset(
    {"write", "write_file", "save", "save_file", "overwrite", "put", "upload",
     "mkdir", "move", "copy", "rename", "mcp__filesystem__write_file",
     # audit r2/r3: file-editing verbs are write-class — make them argument-aware too (a protected
     # path escalates; base stays Write-Local, so adding them can only ESCALATE, never under-protect).
     "edit", "edit_file", "modify", "modify_file", "append", "insert", "patch", "replace"}
)

# audit r4: route the delete/write GROUPS by whole-token too, so spelling variants ("move_file",
# "del_file", "unlink_file", "save_as") are still argument-aware (a protected path arg escalates),
# not only the exact alias names. pay/send stay EXACT-name to avoid ambiguous misrouting
# (e.g. "send_payment" is a payment, "transfer_file" is not). delete checked before write; the two
# token sets are disjoint.
_DELETE_VERB_TOKENS: frozenset[str] = frozenset(
    {"delete", "remove", "rm", "del", "unlink", "drop", "rmdir"}
)
_WRITE_VERB_TOKENS: frozenset[str] = frozenset(
    {"write", "save", "overwrite", "put", "upload", "mkdir", "move", "copy", "rename",
     "edit", "modify", "append", "insert", "patch", "replace",
     # audit r5: file-creation verbs are write-class too (create_file to a protected path escalates).
     "create", "touch", "new", "make", "generate",
     # audit r6: link/symlink to a protected TARGET is a real attack (symlink .ssh/authorized_keys);
     # route them through the argument-aware write rule so a protected target/path escalates.
     "link", "symlink", "softlink", "hardlink", "ln", "junction",
     # audit r8: common write/persist abbreviations.
     "mv", "cp", "store", "persist", "dump", "stash"}
)

# ─── Explicit type→tier map (real BoB tool/action names) ──────────────────────

_TYPE_TIER: dict[str, Tier] = {
    # Read-Only
    "get_server_time": Tier.READ_ONLY,
    "list_backends": Tier.READ_ONLY,
    "read_file": Tier.READ_ONLY,
    "mcp__filesystem__read_file": Tier.READ_ONLY,
    # Write-Local
    "create_project": Tier.WRITE_LOCAL,
    "create_team": Tier.WRITE_LOCAL,
    "cc_edit": Tier.WRITE_LOCAL,
    "chat_with_face": Tier.WRITE_LOCAL,
    "run_council": Tier.WRITE_LOCAL,
    "mcp__filesystem__write_file": Tier.WRITE_LOCAL,
}

# ─── Heuristic token sets (dangerous BEFORE read) ──────────────────────────────
# WHOLE-TOKEN matching (the name is split into alnum tokens; see ``_tokenize``), NOT naive substring.
# This fixes substring false-positives in BOTH directions: "confirm"/"format" no longer trip on the
# "rm" token, and "amount" no longer trips on "mount" — so short, unambiguous privileged verbs
# (rm/kill/mount/...) can be added safely. (audit r3.)

_DANGEROUS_TOKENS: frozenset[str] = frozenset({
    "delete", "remove", "drop", "destroy", "wipe", "purge",
    "pay", "purchase", "buy", "checkout", "send", "email",
    "exec", "shell", "sudo", "rm", "merge", "deploy", "admin", "grant", "revoke",
    # audit r1: unknown destructive verbs must fail CLOSED, not fall to the WRITE_LOCAL default.
    "format", "erase", "truncate", "overwrite", "uninstall", "reset", "factory",
    "flush", "clear", "kill", "terminate", "transfer", "charge", "withdraw",
    # audit r3: privileged system / lifecycle verbs (safe now that matching is whole-token).
    "chmod", "chown", "chgrp", "setcap", "mount", "umount", "unmount", "modprobe",
    "shutdown", "halt", "reboot", "restart", "poweroff", "hibernate", "suspend",
    "systemctl", "service", "kill9", "rmdir", "rmrf", "destroyall",
    # audit r5: code/command execution verbs (high blast radius) — whole-token, low false-positive.
    "execute", "eval", "run", "spawn", "fork", "invoke", "cmd", "command", "script", "sql",
    # audit r9: destructive slang / abbreviations.
    "nuke", "trunc",
})

_READ_TOKENS: frozenset[str] = frozenset({
    "get", "list", "read", "view", "show", "search", "fetch", "query", "describe", "status",
})

# camelCase boundary splitters (run BEFORE lowercasing). Handles "deleteFile" -> delete File and an
# acronym run like "EMAILSend" -> EMAIL Send; an all-caps name ("DELETE") survives as one token.
_CAMEL1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NONALNUM = re.compile(r"[^a-zA-Z0-9]+")


def _tokenize(name: str) -> set[str]:
    """Split an action/tool name into lowercased alnum tokens (snake_case, kebab, dotted, camelCase
    AND all-caps).

    ``"shutdown_system"`` -> {shutdown, system}; ``"deleteFile"`` -> {delete, file};
    ``"DELETE"`` -> {delete}; ``"get_amount"`` -> {get, amount} (NOT {get, mount}).
    Pure, deterministic. (audit r5: all-caps names previously fragmented and dodged the heuristic.)
    """
    s = _CAMEL2.sub(" ", _CAMEL1.sub(" ", name))
    return {tok.lower() for tok in _NONALNUM.split(s) if tok}

# ─── Public API ────────────────────────────────────────────────────────────────

def classify_gui_action(action: Action) -> Tier:
    """Classify a GUI *Action* by its ActionKind (deterministic floor).

    Returns:
        Tier.READ_ONLY for NOOP/SCROLL; Tier.WRITE_LOCAL for KEY/TYPE/CLICK.
    """
    if action.kind in (ActionKind.NOOP, ActionKind.SCROLL):
        return Tier.READ_ONLY
    # All other kinds (KEY, TYPE, CLICK) are local mutations.
    return Tier.WRITE_LOCAL


def classify_tool(
    action_type: str,
    args: Mapping[str, object] | None = None,
    scope: permissions.Scope | None = None,
) -> Tier:
    """Classify a tool/action name to a tier using precedence rules.

    Precedence (highest wins, first match returns):
        1. Floor (unconditional) – membership in ``permissions._ALWAYS_HUMAN_ACTIONS``.
        2. Argument‑aware verb rules for delete, pay, send groups.
        3. Explicit ``_TYPE_TIER`` lookup.
        4. Heuristic fallback (dangerous‑→Full‑Access, read‑→Read‑Only, else Write‑Local).

    Args:
        action_type: the tool or action name (e.g. ``"delete"``, ``"pay"``, ``"get_server_time"``).
        args: call arguments (treated as ``{}`` if ``None``).
        scope: optional job scope (used by path helpers).

    Returns:
        The computed Tier. Never raises.
    """
    # Normalise the action name to lowercase for ALL exact-name matching (floor / aliases / type
    # map). The floor + alias + type-map keys are lowercase, so a case variant ("EMAIL_SEND",
    # "Delete") must not slip past the unconditional floor or the argument-aware groups.
    # (audit r5: case-sensitivity evasion.)
    norm = action_type.strip().lower()

    # Step 1: Floor (unconditional)
    if norm in permissions._ALWAYS_HUMAN_ACTIONS:
        return Tier.FULL_ACCESS

    # Normalise args
    actual_args: Mapping[str, object] = {} if args is None else args
    name_tokens = _tokenize(action_type)  # _tokenize is itself case-robust (all-caps + camelCase)

    # Step 2: Argument‑aware verb rules (only for non‑floor names)
    # 2a. Delete group — escalate if ANY declared path is protected; Write-Local only if EVERY
    # declared path is scratch; else fail closed to Full-Access. (audit r2: extract destination
    # keys too; audit r4: route spelling variants like "del_file"/"unlink_file" by whole-token.)
    if norm in _DELETE_ALIASES or (name_tokens & _DELETE_VERB_TOKENS):
        paths = _extract_paths(actual_args)
        if any(is_protected_path(p, scope) for p in paths):
            return Tier.FULL_ACCESS
        if paths and all(_is_scratch_path(p, scope) for p in paths):
            return Tier.WRITE_LOCAL
        # Fail closed – ambiguous / missing / mixed path → Full‑Access
        return Tier.FULL_ACCESS

    # 2a'. Write group (argument-aware ESCALATION only; base stays Write-Local). A write to ANY
    # protected path escalates; a write with no/benign path is a local mutation. (audit r4: also
    # routes spelling variants like "move_file"/"save_as" by whole-token.)
    if norm in _WRITE_ALIASES or (name_tokens & _WRITE_VERB_TOKENS):
        paths = _extract_paths(actual_args)
        if any(is_protected_path(p, scope) for p in paths):
            return Tier.FULL_ACCESS
        return Tier.WRITE_LOCAL

    # 2b. Pay group
    if norm in _PAY_ALIASES:
        if _is_zero_amount(actual_args.get("amount")):
            return Tier.SOCIAL
        # Non‑zero or missing amount → Full‑Access
        return Tier.FULL_ACCESS

    # 2c. Send group
    if norm in _SEND_ALIASES:
        # Defense in depth (audit r8): a send/post carrying a protected file path escalates first.
        if any(is_protected_path(p, scope) for p in _extract_paths(actual_args)):
            return Tier.FULL_ACCESS
        if _is_self_recipient(actual_args):
            return Tier.WRITE_LOCAL
        return Tier.SOCIAL

    # Step 3: Explicit type map
    if norm in _TYPE_TIER:
        return _TYPE_TIER[norm]

    # Step 4: Heuristic fallback — WHOLE-TOKEN matching (dangerous before read; safe default
    # Write-Local). This is a best-effort net for UNKNOWN names; the real second safety layer is
    # route_action -> evaluate_action (an unknown action with no scope -> 'human', with a scope ->
    # 'gate'/critic — never silently 'auto').
    if name_tokens & _DANGEROUS_TOKENS:
        return Tier.FULL_ACCESS
    if name_tokens & _READ_TOKENS:
        return Tier.READ_ONLY
    return Tier.WRITE_LOCAL


def resolve_tier(
    target: Action | str,
    *,
    args: Mapping[str, object] | None = None,
    scope: permissions.Scope | None = None,
) -> Tier:
    """Unified classification dispatcher.

    Args:
        target: an ``Action`` instance → ``classify_gui_action``.
                 a ``str`` (tool/action name) → ``classify_tool``.

    Raises:
        TypeError: if *target* is neither an ``Action`` nor a ``str``.
    """
    if isinstance(target, Action):
        return classify_gui_action(target)
    if isinstance(target, str):
        return classify_tool(target, args=args, scope=scope)
    raise TypeError(
        f"resolve_tier expects Action or str, got {type(target).__name__}"
    )


def requires_human(tier: Tier) -> bool:
    """Return True iff the given tier is ``Tier.FULL_ACCESS`` (the only human‑required tier)."""
    return tier is Tier.FULL_ACCESS


def route_action(
    action_type: str,
    scope: permissions.Scope | None = None,
    *,
    args: Mapping[str, object] | None = None,
) -> str:
    """Route an action to a gate destination, returning ``"auto"``, ``"gate"``, or ``"human"``.

    Full‑Access always returns ``"human"``, even if the scope would otherwise allow the action.
    Otherwise delegates to ``permissions.evaluate_action``.

    Args:
        action_type: the tool/action name.
        scope: optional job scope.
        args: call arguments (passed to ``classify_tool``).

    Returns:
        One of ``"auto"``, ``"gate"``, ``"human"``.
    """
    tier = classify_tool(action_type, args=args, scope=scope)
    if tier is Tier.FULL_ACCESS:
        return "human"
    return permissions.evaluate_action(action_type, scope)


def is_protected_path(path: str, scope: permissions.Scope | None = None) -> bool:
    """Pure‑string analysis: determine whether a path is considered protected.

    Returns ``True`` if:
        - *path* is empty / falsy (fail closed).
        - the path contains a ``..`` traversal segment (fail closed — a traversal can escape any
          scratch sandbox; audit r1 path-traversal bypass).
        - any segment (split on ``/`` and ``\\``, lowercased) is in ``_PROTECTED_SEGMENTS``.
        - the lowercased path ends with a suffix in ``_PROTECTED_SUFFIXES``.
        - the path is an absolute UNIX system path (``_SYSTEM_PREFIXES`` / ``_SYSTEM_ROOTS``).
        - *scope* is not ``None`` and ``permissions.evaluate_path(path, scope) == "human"``
          (a scope may only ESCALATE — its ``may_not_touch`` — never de-escalate).

    Never touches the filesystem (string-only).
    """
    # Fail closed: treat missing path as protected
    if not path:
        return True

    # Normalize separators + strip outer whitespace; lower for case-insensitive matching. Stripping
    # closes a whitespace-padded leading-space bypass (e.g. " /etc/passwd"). (audit r3.)
    # Collapse repeated slashes so "//etc/passwd" normalizes to "/etc/passwd" and can't dodge the
    # system-prefix check. (audit r4.)
    norm = re.sub(r"/+", "/", path.replace("\\", "/").strip())
    # A whitespace-only / now-empty path is ambiguous (resolves to cwd) -> fail closed. (audit r7.)
    if not norm:
        return True
    lower = norm.lower()
    # The filesystem root ("/") or a bare drive root ("c:" / "c:/") -> always protected; a write/delete
    # at the root is catastrophic. (audit r9.)
    if lower == "/" or re.fullmatch(r"[a-z]:/?", lower):
        return True
    # Per-segment STRIPPED (so a padded traversal ".. "/"  .." is caught) and "."-segments dropped
    # so "/etc/./passwd" still matches. (audit r3/r4.)
    segments = [seg.strip() for seg in lower.split("/") if seg.strip() != "."]

    # Traversal -> fail closed (a "scratch/../.." path could otherwise look scratch).
    if ".." in segments:
        return True

    # Sensitive segment anywhere.
    if any(seg in _PROTECTED_SEGMENTS for seg in segments):
        return True

    # Sensitive suffix.
    if any(lower.endswith(suffix) for suffix in _PROTECTED_SUFFIXES):
        return True

    # Absolute UNIX system path (prefix match, so relative project dirs are not false-flagged).
    if lower.startswith(_SYSTEM_PREFIXES) or lower.rstrip("/") in _SYSTEM_ROOTS:
        return True

    # Scope ESCALATION only (may_not_touch -> human). A scope never de-escalates the tier.
    if scope is not None and permissions.evaluate_path(path, scope) == "human":
        return True

    return False


# ─── Private helpers ───────────────────────────────────────────────────────────

def _is_scratch_path(path: str, scope: permissions.Scope | None = None) -> bool:
    """Return ``True`` if the path is a recognised scratch/temp location (STATIC only).

    Defined purely by ``_SCRATCH_SEGMENTS`` membership. NOTE (audit r1): a scope's ``may_touch``
    is deliberately NOT consulted here — a permissive scope must never DE-escalate an intrinsic
    tier (that hole let ``delete('/etc/passwd')`` under ``may_touch=['*']`` look scratch). The
    scope's clearance is applied at the ROUTING layer (``route_action`` -> ``evaluate_action``),
    not at tier classification. A traversal path is never scratch (caller checks
    :func:`is_protected_path` first, which already fails such paths closed).
    """
    segments = [seg.strip() for seg in path.replace("\\", "/").split("/")]
    if ".." in segments:
        return False
    return any(seg.lower() in _SCRATCH_SEGMENTS for seg in segments)


def _is_zero_amount(amount: object) -> bool:
    """Return ``True`` iff *amount* can be parsed as a numeric zero.

    Returns ``False`` if *amount* is ``None``, a bool, non‑numeric, or fails to parse
    (fail closed, because a non‑zero amount is not zero). The explicit ``bool`` reject is
    defensive: a ``bool`` is an ``int`` subclass, so it is never a meaningful payment amount —
    a ``pay(amount=False)`` must stay Full-Access, never de-escalate. (audit r3.)

    Uses ``Decimal`` for EXACT zero detection — ``float("1e-1000")`` underflows to ``0.0`` and would
    wrongly de-escalate a tiny non-zero payment to Social; ``Decimal("1e-1000") != 0``. (audit r8.)
    """
    if amount is None or isinstance(amount, bool):
        return False
    try:
        return Decimal(str(amount)) == 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def _is_self_recipient(args: Mapping[str, object]) -> bool:
    """Determine whether the recipient is the sender (self‑send).

    Rules:
        1. Extract recipient from ``args["recipient"]`` or ``args["to"]``.
        2. If recipient is empty → ``False``.
        3. If recipient is a self‑marker (``"self"``, ``"me"``, ``"myself"``) → ``True``.
        4. Otherwise, obtain sender from ``args["from"]`` or ``args["sender"]``.
           If sender is non‑empty and equals the recipient (case‑insensitive, stripped) → ``True``.
    """
    recipient_raw = args.get("recipient") or args.get("to") or ""
    if not isinstance(recipient_raw, str):
        return False
    recipient = recipient_raw.strip().lower()
    if not recipient:
        return False
    if recipient in {"self", "me", "myself"}:
        return True

    sender_raw = args.get("from") or args.get("sender") or ""
    if not isinstance(sender_raw, str):
        return False
    sender = sender_raw.strip().lower()
    return bool(sender) and recipient == sender
