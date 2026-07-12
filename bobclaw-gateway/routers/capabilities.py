"""BoBClaw Gateway — capabilities registry endpoint (read-only aggregation).

``GET /capabilities`` fans out three concurrent GETs to core's existing registry read
surfaces (``/api/faces`` + ``/api/backends`` + ``/api/models/available``) and composes them
into a single read-only document that BOTH the TUI slash-command palette (``CapabilitiesClient``
seam) and the desktop-app ``/`` palette consume from ONE call — build it once, both read it.

JWT-gated by the gateway ``auth_middleware`` like every other route (``/capabilities`` is not on
the public allowlist). Read-only: GETs only, no mutation, no session/app-state writes.

Degrade posture (palette-backing): a per-component failure (a core connection error, a non-200
status, or a JSON decode error) degrades THAT component to its empty value plus a top-level
``warnings`` entry; the endpoint still returns 200 with whatever it composed. Only when ALL
three component fetches fail does it surface 502 (mirrors the sibling ``routing_view`` posture).
"""
import asyncio
import json

import aiohttp
from aiohttp import web

from config import config

router = web.RouteTableDef()

# Empty fallback per component so a partial outage still composes a valid document.
_EMPTY = {
    "faces": [],
    "backends": {"items": [], "roles": []},
    "models": [],
}

# U2 (Decision D10): display-metadata keys guaranteed on every faces[] entry so the
# client sees a STABLE schema — present ⇒ passed through verbatim, absent ⇒ null.
_FACE_DISPLAY_FIELDS = ("display_name", "blurb", "simple_slot")


def _load_actions() -> tuple[list, str | None]:
    """Return the U3 action registry as ``(entries, warning)``.

    The action registry (SPEC §3 / D4) is STATIC core-side data — not a runtime read surface —
    so, like ``core.permissions`` elsewhere in the gateway, it is imported directly rather than
    fetched over HTTP. On any failure it degrades to ``([], warning)`` so the ``actions`` section
    stays additive and null-safe (the endpoint never fails on its account).
    """
    try:
        from core.actions import get_default_registry

        return get_default_registry().as_payload(), None
    except Exception as exc:  # noqa: BLE001 — never let the actions section break /capabilities
        return [], f"actions: {type(exc).__name__}: {exc}"[:160]


async def _fetch_json(
    session: aiohttp.ClientSession, url: str, component: str
) -> tuple[object, str | None]:
    """GET *url* and return ``(parsed_data, warning)``.

    On success ``warning`` is ``None``. On any failure (connection error, non-200 status, or a
    non-JSON body) returns the component's empty value plus a short human-readable warning
    string, so the caller degrades that component instead of failing the whole request.
    """
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return _EMPTY[component], f"{component}: HTTP {resp.status}"
            data = await resp.json()
            return data, None
    except (aiohttp.ClientError, ValueError) as exc:
        return _EMPTY[component], f"{component}: {type(exc).__name__}: {exc}"[:160]


@router.get("/capabilities")
async def get_capabilities(request: web.Request) -> web.Response:
    """Serve the aggregated live registry (faces / backends / capabilities), read-only.

    Fans out three concurrent GETs to core over ONE shared session, merges the backends by name
    (union of ``/api/backends`` cost caps + ``/api/models/available`` availability), and returns
    a stable, sorted JSON document. Partial failures degrade to a ``warnings`` list; a total core
    outage surfaces 502.
    """
    # Read CORE_URL at request time (NOT module import) — tests and launchers override
    # config.CORE_URL, exactly as the sibling faces/models/routing_view routers do.
    core_base = config.CORE_URL.rstrip("/")

    async with aiohttp.ClientSession() as session:
        (faces_data, faces_warn), (backends_data, backends_warn), (models_data, models_warn) = (
            await asyncio.gather(
                _fetch_json(session, f"{core_base}/api/faces", "faces"),
                _fetch_json(session, f"{core_base}/api/backends", "backends"),
                _fetch_json(session, f"{core_base}/api/models/available", "models"),
            )
        )

    warnings = [w for w in (faces_warn, backends_warn, models_warn) if w]

    # Total core outage → 502 (mirrors routing_view); a partial outage degrades below.
    if faces_warn and backends_warn and models_warn:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": "capabilities: core registry unreachable"}),
            content_type="application/json",
        )

    # Pass faces through, but additively guarantee the U2 display-metadata keys on
    # every entry (null-safe: a face lacking a key gets it as null). Never mutates the
    # source dict; non-dict entries (defensive) pass through untouched.
    faces = [
        {**f, **{k: f.get(k) for k in _FACE_DISPLAY_FIELDS}} if isinstance(f, dict) else f
        for f in (faces_data if isinstance(faces_data, list) else [])
    ]
    backends_items = backends_data.get("items", []) if isinstance(backends_data, dict) else []
    backends_roles = backends_data.get("roles", []) if isinstance(backends_data, dict) else []
    models_list = models_data if isinstance(models_data, list) else []

    # Merge one entry per backend name = union of availability (models/available) + cost caps
    # (/api/backends). Tolerate a backend present in only one source; never KeyError.
    merged: dict[str, dict] = {}
    for m in models_list:
        if not isinstance(m, dict):
            continue
        name = m.get("backend")
        if not name:
            continue
        merged[name] = {
            "backend": name,
            "available": bool(m.get("available", False)),
            "model": m.get("model"),
            "max_usd_per_worker": None,
            "max_fanout_width": None,
        }
    for item in backends_items:
        if not isinstance(item, dict):
            continue
        name = item.get("backend")
        if not name:
            continue
        entry = merged.get(name)
        if entry is None:
            entry = merged[name] = {
                "backend": name,
                "available": False,
                "model": None,
                "max_usd_per_worker": None,
                "max_fanout_width": None,
            }
        entry["max_usd_per_worker"] = item.get("max_usd_per_worker")
        entry["max_fanout_width"] = item.get("max_fanout_width")

    backends_list = sorted(merged.values(), key=lambda e: e["backend"])
    available_backends = [b["backend"] for b in backends_list if b["available"]]

    # U3 (Decision D4): additive ``actions`` section — the static core-side action registry
    # (§3), served alongside faces/backends so the ONE /capabilities call also backs the command
    # palette, the helper bubble, and voice. Null-safe: a load failure degrades to [] + a warning.
    actions, actions_warn = _load_actions()
    if actions_warn:
        warnings.append(actions_warn)

    doc: dict = {
        "faces": faces,
        "backends": backends_list,
        "actions": actions,
        "capabilities": {
            "roles": backends_roles,
            "face_count": len(faces),
            "backend_count": len(backends_list),
            "available_backends": available_backends,
            "action_count": len(actions),
        },
    }
    if warnings:
        doc["warnings"] = warnings

    return web.json_response(doc)
