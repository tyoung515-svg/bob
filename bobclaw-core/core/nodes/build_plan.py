"""BoBClaw build pipeline — P0: contract planning + the build-empty gate.

``plan_contracts_node`` is the entry of the agentic build loop (productionizing
``demo_variant_b.py``). It is the ONLY build-pipeline node that touches the network
or the filesystem subprocess; the heavy lifting is the pure/deterministic
:mod:`core.build.contracts` + :mod:`core.build.skeleton`.

Sequence (all before any worker runs — that is P1):
  1. Ask the apex backend (via the standard ``_send_to_backend`` seam) for the
     SKELETON: ~N unit contracts as JSON.
  2. Parse + validate them (tolerant of truncation) → ``build_contracts``.
  3. Write the deterministic stub package + pytest suite + CLI into a per-turn
     SANDBOX dir under ``BUILD_WORKSPACE_ROOT`` (outside the repo tree).
  4. Run the build-empty gate (imports clean + tests collect) and record it on
     ``verify_report``. A deterministic skeleton that won't build is FAIL-LOUD (our
     bug / a pathological signature), never masked.

Gating: this node is wired into the graph behind the ``build_contracts`` field
(P1), so a turn that never plans contracts is byte-identical to today. It sets
``build_contracts`` (the gate the downstream build path keys on), ``build_workspace``
(the sandbox), ``verify_report`` (the skeleton gate), and ``repair_round = 0``.

NOTE on token budget: the apex call uses ``_send_to_backend`` (the mockable seam),
whose claude_api branch caps ``max_tokens`` at 4096. That comfortably fits the small
N this pipeline targets (the live E2E is N=10); a large-N skeleton (the demo's 100)
overflowed 4096 and used a direct large-budget client call — a budget knob is a
follow-up if big jobs land here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import core.config as _config
import core.teams as teams
from core.build import contracts, skeleton
from core.build.contracts import ALLOWED_IMPORTS
from core.nodes.execute import _send_to_backend
from core.permissions import is_path_within

logger = logging.getLogger(__name__)

_IMPORTS_LINE = ", ".join(ALLOWED_IMPORTS)


def _contracts_prompt(objective: str, n: int) -> str:
    """The contract-first skeleton prompt: objective + the exact JSON contract shape.

    The stdlib-only constraint names the SAME modules the skeleton HEADER imports
    (single-sourced from ``ALLOWED_IMPORTS``), so contracts, skeleton, and the P1
    worker prompt can never drift on what is "already imported".
    """
    return (
        f"{objective}\n\n"
        f"Design the toolkit as PURE Python functions using ONLY these already-"
        f"imported standard-library modules (do NOT import anything else): "
        f"{_IMPORTS_LINE}.\n\n"
        f'Return ONLY compact JSON (no prose): {{"units": [ {{"name": <identifier>, '
        f'"signature": "name(params)", "doc": <terse one line>, "cases": '
        f'[{{"args": [...], "expect": <json value>}}, ...]}} ]}} with EXACTLY {n} '
        f"units. Each function pure, JSON-serializable I/O (str/int/float/bool/list/"
        f"dict), no I/O; exactly 2 cases each with CORRECT expected values; unique "
        f"names; keep it terse to fit."
    )


def _safe_segment(value: str) -> str:
    """Sanitize a path segment to identifier-ish chars (defense-in-depth; P3 hardens).

    The conversation id is operator-supplied; clamp it to ``[A-Za-z0-9_-]`` so it can
    never inject a separator / ``..`` into the workspace path. (Contract NAMES are
    already identifier-only via ``contracts.coerce_units``.)
    """
    seg = re.sub(r"[^a-zA-Z0-9_-]", "_", (value or "").strip())
    return seg[:64] or "anon"


def _make_workspace(state: dict) -> Path:
    """Create + return a fresh per-turn sandbox dir under ``BUILD_WORKSPACE_ROOT``.

    ``<root>/<conversation>/<UTC-stamp>-<rand>`` — unique per turn so concurrent /
    repeated builds never collide. Read the root LATE off the module so tests can
    monkeypatch ``core.config.BUILD_WORKSPACE_ROOT`` to a tmp dir. P3: the resolved
    path is HARD-validated to be inside the root BEFORE mkdir (fail-closed on any
    escape — defense-in-depth atop ``_safe_segment`` + the identifier-only contract
    names); raises ValueError on escape so the caller fails loud without writing.
    """
    root = _config.BUILD_WORKSPACE_ROOT
    conv = _safe_segment(state.get("conversation_id") or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ws = Path(root) / conv / f"{stamp}-{uuid.uuid4().hex[:8]}"
    if not is_path_within(str(ws), str(root)):
        raise ValueError(f"build workspace {ws} escapes sandbox root {root}")
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _build_scope(workspace: Path) -> dict:
    """The Gate-Router blast radius for a build turn — an AUDIT / governance RECORD, not
    runtime subprocess enforcement. It declares the intended radius (touch only the
    sandbox; repo off-limits) for the Gate Router + a future scope-aware impl critic;
    the executing build subprocess is NOT actually filesystem-jailed by it (see
    contracts.is_safe_impl / skeleton.build_env for what IS enforced). Threaded onto
    state + into the build Sends. Promotion out of the sandbox / any real merge stays
    ALWAYS-HUMAN (no code path auto-promotes a built app)."""
    try:
        repo_root = str(Path(_config.config.CC_PROJECT_DIR).resolve())
    except (OSError, ValueError, RuntimeError):
        repo_root = _config.config.CC_PROJECT_DIR
    return {
        "may_touch": [str(workspace)],
        "may_not_touch": [repo_root],
        "auto_actions": [],
    }


async def plan_contracts_node(state: dict) -> dict:
    """Plan contracts, write the deterministic skeleton, run the build-empty gate.

    Returns a state delta. On success: ``build_contracts`` + ``build_workspace`` +
    ``verify_report`` (skeleton phase) + ``repair_round = 0``. On no-valid-contracts
    or a skeleton that won't build: ``error`` is set (fail-loud) so the turn surfaces
    the problem instead of fanning out workers over a broken skeleton.
    """
    objective = state.get("task") or ""
    # Apex (planner) backend: the active team's apex role (e.g. demo-fleet → Opus),
    # falling back to the turn's resolved backend. The build WORKER backend is
    # resolved separately at fan-out (dispatch), so apex≠worker holds (Opus plans,
    # DeepSeek builds) without a per-turn single-backend collision.
    backend = teams.role_backend(state.get("team"), "apex") or state.get("backend") or "local"
    n = int(state.get("build_units") or _config.BUILD_DEFAULT_UNITS)

    raw = await _send_to_backend(
        [{"role": "user", "content": _contracts_prompt(objective, n)}], backend
    )
    units = contracts.parse_units(raw, n)
    if not units:
        logger.warning("build plan_contracts: no valid contracts parsed from apex skeleton")
        return {"error": "build pipeline: apex emitted no valid contracts; aborting build"}

    try:
        workspace = _make_workspace(state)
    except ValueError as exc:
        # Sandbox containment breach (P3) — fail loud, write nothing.
        logger.error("build plan_contracts: %s", exc)
        return {"build_contracts": units, "error": f"build pipeline: {exc}"}

    if len(units) < n:
        # Surface a truncated/under-delivered skeleton (e.g. the apex JSON was cut off
        # and salvage recovered fewer than requested) — don't silently build a smaller app.
        logger.warning("build plan_contracts: requested %d contracts, got %d "
                       "(apex reply may have been truncated)", n, len(units))

    scope = _build_scope(workspace)
    # write + the build-empty subprocess gate would block the event loop — offload.
    await asyncio.to_thread(skeleton.write_app, workspace, units, {})
    builds_empty = await asyncio.to_thread(skeleton.build_empty_ok, workspace)
    collected = await asyncio.to_thread(skeleton.collect_tests, workspace)
    report = {
        "phase": "skeleton",
        "builds_empty": builds_empty,
        "tests_collected": collected,
        "units_valid": len(units),
        "units_requested": n,
    }

    if not builds_empty:
        # Fail loud: a DETERMINISTIC skeleton that won't import is our codegen bug or
        # a pathological signature the parser admitted — surface it, never mask. (The
        # impl/test pass-fail story belongs to the P2 verify gate, not here.)
        logger.error(
            "build plan_contracts: deterministic skeleton failed the build-empty gate "
            "(%d contracts, workspace=%s)", len(units), workspace,
        )
        return {
            "build_contracts": units,
            "build_workspace": str(workspace),
            "scope": scope,
            "verify_report": report,
            "error": "build pipeline: deterministic skeleton failed to build empty",
        }

    logger.info(
        "build plan_contracts: %d contracts, builds_empty=%s tests_collected=%d "
        "(workspace=%s)", len(units), builds_empty, collected, workspace,
    )
    return {
        "build_contracts": units,
        "build_workspace": str(workspace),
        "scope": scope,
        "verify_report": report,
        "repair_round": 0,
    }
