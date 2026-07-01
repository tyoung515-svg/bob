from __future__ import annotations

"""Parse / validate / format GUI actions (PURE, stdlib-only).

``parse_action`` is strict and deterministic — any malformed input returns ``None``
(the PARSE_ERROR landmine probe, unified §5). ``format_action`` is the canonical
single-line form used both as the stuck-detector dedup key and as the FakeSurface
transition key, and round-trips through ``parse_action`` for simple valid actions.
Never uses eval/exec.
"""

import re

from core.gui.types import Action, ActionKind

_VALID_ARGS = frozenset({"target", "text", "key", "coord", "direction", "amount"})
_SCROLL_DIRS = frozenset({"up", "down", "left", "right"})


def parse_action(text: str) -> Action | None:
    """Parse ``"kind(arg=val, ...)"`` into an :class:`Action`; ``None`` on any malformiation.

    ``kind`` is case-insensitive and must be a valid :class:`ActionKind`. Supported args:
    ``target``/``text``/``key``/``direction`` (str), ``coord`` (``"x,y"`` or ``"(x,y)"``),
    ``amount`` (int). Values may be single/double quoted or bare. Unknown kind/arg, bad
    coord, non-int amount, or unbalanced parens/quotes → ``None``.
    """
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*(?P<kind>\w+)\s*\(\s*(?P<args>.*?)\s*\)\s*$", text, re.IGNORECASE)
    if not m:
        return None
    try:
        kind = ActionKind(m.group("kind").lower())
    except ValueError:
        return None

    args_str = m.group("args").strip()
    if not args_str:
        return Action(kind=kind)

    parts = _split_args(args_str)
    if parts is None:
        return None

    parsed: dict[str, object] = {}
    for arg in parts:
        eq = arg.find("=")
        if eq == -1:
            return None
        key = arg[:eq].strip().lower()
        if key not in _VALID_ARGS:
            return None
        value = _unquote(arg[eq + 1:].strip())
        if value is None:
            return None
        if key == "coord":
            coord = _parse_coord(value)
            if coord is None:
                return None
            parsed[key] = coord
        elif key == "amount":
            try:
                parsed[key] = int(value)
            except ValueError:
                return None
        else:
            parsed[key] = value

    return Action(
        kind=kind,
        target=parsed.get("target", ""),
        text=parsed.get("text", ""),
        key=parsed.get("key", ""),
        coord=parsed.get("coord", None),
        direction=parsed.get("direction", ""),
        amount=parsed.get("amount", 0),
    )


def _split_args(args_str: str) -> list[str] | None:
    """Split on top-level commas, respecting quotes AND parens (so ``coord=(1,2)`` stays
    one arg). ``None`` if quotes or parens are unbalanced."""
    args: list[str] = []
    current: list[str] = []
    in_quote: str | None = None
    depth = 0
    i = 0
    while i < len(args_str):
        ch = args_str[i]
        if in_quote:
            if ch == "\\" and i + 1 < len(args_str):
                current.append(args_str[i + 1])
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            current.append(ch)
        else:
            if ch in ('"', "'"):
                in_quote = ch
                current.append(ch)
            elif ch == "(":
                depth += 1
                current.append(ch)
            elif ch == ")":
                depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        i += 1
    args.append("".join(current).strip())
    if in_quote is not None or depth != 0:
        return None
    return args


def _unquote(value: str) -> str | None:
    """Strip a matching pair of surrounding quotes; bare values pass through unchanged."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_coord(coord_str: str) -> tuple[int, int] | None:
    """Parse ``"x,y"`` or ``"(x,y)"`` → ``(int, int)``; ``None`` on failure."""
    s = coord_str.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    parts = s.split(",")
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0].strip()), int(parts[1].strip()))
    except ValueError:
        return None


def validate_action(a: Action) -> tuple[bool, str]:
    """Structural sufficiency → ``(ok, reason)``; ``reason`` is empty when ok."""
    if a.kind == ActionKind.CLICK:
        if not a.target and a.coord is None:
            return (False, "CLICK requires target or coord")
    elif a.kind == ActionKind.TYPE:
        if not a.text:
            return (False, "TYPE requires non-empty text")
    elif a.kind == ActionKind.SCROLL:
        if a.direction not in _SCROLL_DIRS:
            return (False, f"SCROLL direction must be one of up/down/left/right, got '{a.direction}'")
    elif a.kind == ActionKind.KEY:
        if not a.key:
            return (False, "KEY requires non-empty key")
    elif a.kind == ActionKind.NOOP:
        pass
    else:  # pragma: no cover - ActionKind is exhaustive
        return (False, f"unknown action kind: {a.kind}")
    return (True, "")


_DELIMS = frozenset(",()\"'\\")


def _fmt_val(v: str) -> str:
    """Quote+escape a string arg value iff it carries a delimiter or edge whitespace,
    so ``parse_action(format_action(a))`` round-trips losslessly for ANY valid value."""
    if v.strip() != v or any(c in _DELIMS for c in v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def format_action(a: Action) -> str:
    """Canonical ``"kind(k=v, ...)"`` form (args sorted, only non-default fields).

    This is BOTH the stuck-detector dedup key and the FakeSurface transition key, so it
    must be stable AND lossless: ``parse_action(format_action(a)) == a`` for every valid
    Action (string values carrying delimiters are quoted+escaped).
    """
    fields: list[tuple[str, str]] = []
    if a.amount != 0:
        fields.append(("amount", str(a.amount)))
    if a.coord is not None:
        fields.append(("coord", f"({a.coord[0]},{a.coord[1]})"))
    if a.direction:
        fields.append(("direction", _fmt_val(a.direction)))
    if a.key:
        fields.append(("key", _fmt_val(a.key)))
    if a.target:
        fields.append(("target", _fmt_val(a.target)))
    if a.text:
        fields.append(("text", _fmt_val(a.text)))
    fields.sort(key=lambda kv: kv[0])
    inner = ", ".join(f"{k}={v}" for k, v in fields)
    return f"{a.kind.value}({inner})"
