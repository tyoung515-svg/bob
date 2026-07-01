from __future__ import annotations

import hashlib
import json

from core.gui.types import A11yNode, Frame, FrameDiff

# Sentinel hash used when data is None or empty.
_SENTINEL_HASH = hashlib.sha256(b"").hexdigest()


def hash_bytes(data: bytes | None) -> str:
    """SHA‑256 hex digest of *data*.

    Returns a fixed sentinel hash (sha256(b"") ) if *data* is ``None`` or ``b""``.
    """
    if data is None or data == b"":
        return _SENTINEL_HASH
    return hashlib.sha256(data).hexdigest()


def frame_signature(frame: Frame) -> str:
    """Deterministic, order-independent total-state signature of a frame.

    The stuck detector's "did anything observable change" key. Hashes the image hash,
    the size, and every a11y node's full identity (node_id, role, name, value, bounds),
    sorted so node order doesn't matter. It deliberately includes node_id so a node's
    identity change counts as a change (consistent with frame_diff / a11y_index, which
    key on node_id); seq is excluded so a re-capture of an unchanged surface reads as
    no-progress.
    """
    # JSON-encode per node (delimiter-safe: a '|'/newline inside any field can't forge a
    # boundary), sort the encoded strings (order-independent), then hash the whole payload.
    node_jsons = sorted(
        json.dumps(
            [node.node_id, node.role, node.name, node.value,
             list(node.bounds) if node.bounds is not None else None],
            ensure_ascii=False,
        )
        for node in frame.a11y
    )
    payload = json.dumps([frame.image_hash, list(frame.size), node_jsons], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def a11y_index(frame: Frame) -> dict[str, A11yNode]:
    """Build a dictionary keyed by *node_id* (if truthy) else ``"{role}:{name}"``.

    Later nodes overwrite earlier ones when duplicate keys occur (last‑wins).
    """
    idx: dict[str, A11yNode] = {}
    for node in frame.a11y:
        key = node.node_id if node.node_id else f"{node.role}:{node.name}"
        idx[key] = node
    return idx


def frame_diff(prev: Frame | None, cur: Frame) -> FrameDiff:
    """Compute the cheap *did‑anything‑happen* diff between two frames.

    If *prev* is ``None``, every change flag is set to ``True`` and the *added*
    list contains all keys from the current frame's a11y index.
    """
    if prev is None:
        added = tuple(sorted(a11y_index(cur).keys()))
        return FrameDiff(
            changed=True,
            pixel_changed=True,
            a11y_changed=True,
            added=added,
            removed=(),
            text_changed=bool(cur.a11y),
        )

    pixel_changed = prev.image_hash != cur.image_hash
    pa = a11y_index(prev)
    ca = a11y_index(cur)

    pa_keys = set(pa.keys())
    ca_keys = set(ca.keys())

    added = sorted(ca_keys - pa_keys)
    removed = sorted(pa_keys - ca_keys)

    common = pa_keys & ca_keys
    text_changed = any(pa[k].value != ca[k].value for k in common)

    a11y_changed = bool(added or removed or text_changed)
    changed = pixel_changed or a11y_changed

    return FrameDiff(
        changed=changed,
        pixel_changed=pixel_changed,
        a11y_changed=a11y_changed,
        added=tuple(added),
        removed=tuple(removed),
        text_changed=text_changed,
    )


def a11y_contains(
    frame: Frame,
    *,
    node_id: str = "",
    name: str = "",
    value_substr: str = "",
) -> bool:
    """Return ``True`` iff at least one a11y node matches **all** non‑empty filters.

    Filters:
    * *node_id* — exact match against ``A11yNode.node_id``.
    * *name* — exact match against ``A11yNode.name``.
    * *value_substr* — substring match against ``A11yNode.value``.

    If every filter is empty, returns ``False``.
    """
    # No filter supplied → nothing can match.
    if not node_id and not name and not value_substr:
        return False

    for node in frame.a11y:
        if node_id and node.node_id != node_id:
            continue
        if name and node.name != name:
            continue
        if value_substr and value_substr not in node.value:
            continue
        return True
    return False
