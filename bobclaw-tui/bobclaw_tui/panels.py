"""BoBClaw TUI — pure formatters for the poll-fed panes (Lane 1c, tested data layer).

The Routing and Approvals panes are fed by REST polls (``/routing-view`` and
``/approvals``), not the ``/ws/monitor`` stream, so they don't belong in the frame
reducer (``monitor_state``). But the same discipline applies: the JSON→lines shaping is
PURE (no I/O, no clock), so it's CI-tested without a terminal — ``app.py`` only does the
aiohttp poll + ``Static.update``. Both formatters are forward-compatible: a missing/garbled
payload renders an ``unavailable`` line instead of raising, so a gateway/Postgres blip
never crashes the cockpit.
"""
from __future__ import annotations


def _err_line(payload, noun: str) -> list[str]:
    err = payload.get("error") if isinstance(payload, dict) else None
    return [f"{noun} unavailable: {err}" if err else f"{noun} unavailable"]


def routing_lines(view: dict, *, max_faces: int = 14, ascii_mode: bool = False) -> list[str]:
    """Shape the JOAT ``/routing-view`` JSON into cockpit lines.

    Header = active team + probe posture + face/rerouted counts. Then the faces,
    **rerouted-first** (``resolved_backend != preferred_backend`` — the interesting
    rows a supervisor watches: a health-walk or team override actually moved routing),
    capped at ``max_faces`` with a ``+N more`` tail. ``ascii_mode`` swaps the VOCABULARY §4
    glyphs (``↝→->``, ``🔧→[t]``, ``…→...``) for the ``/ascii`` toggle; the default is
    byte-identical to before so every existing caller/test is unchanged.
    """
    if not isinstance(view, dict) or "faces" not in view:
        return _err_line(view, "routing")
    faces = view.get("faces") or []

    def _rerouted(f: dict) -> bool:
        return f.get("resolved_backend") != f.get("preferred_backend")

    rerouted = [f for f in faces if _rerouted(f)]
    rest = [f for f in faces if not _rerouted(f)]
    live = bool(view.get("live_probe"))
    g_reroute = "->" if ascii_mode else "↝"
    g_tool = "[t]" if ascii_mode else "🔧"
    g_ell = "..." if ascii_mode else "…"
    head = [
        f"team: {view.get('active_team') or '(default · per-face)'}",
        f"probe: {'live · health-walk' if live else 'declared · not-checked'}",
        f"faces: {len(faces)}   rerouted: {len(rerouted)}",
        "",
    ]
    rows = []
    for f in (rerouted + rest)[:max_faces]:
        res = f.get("resolved_backend") or "?"
        mark = g_reroute if _rerouted(f) else " "
        tool = g_tool if f.get("tool_capable") else " "
        rows.append(f"{mark}{tool} {str(f.get('id', '?'))[:20]:<20} → {res}")
    if len(faces) > max_faces:
        rows.append(f"{g_ell} +{len(faces) - max_faces} more")
    return head + rows


def fmt_tokens(n: int) -> str:
    """Compact token count: 940 → '940', 4200 → '4.2k', 1_300_000 → '1.3M'."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def health_line(
    health: dict | None, conn_status: str, conn_attempts: int, *, ascii_mode: bool = False
) -> str:
    """The header connection-health cell (T2): ``● monitor: <state>``.

    Combines the reducer's transport-error signal (``MonitorState.health()``) with the
    ``monitor_client`` connection/backoff state (``ConnState``) into one honest state so
    the user can tell **idle** (live, no traffic) from **socket-dead** (reconnecting) from
    **Redis-down** (a ``redis_unavailable`` error frame while the socket is still up):

    - a transport error frame present → ``DOWN:<code>`` (alert) — the most severe, wins
      even when the socket itself is connected (Redis can be down under a live WS);
    - else the socket is ``live`` (success);
    - else it is ``reconnecting(n)`` / ``connecting`` (warn) with the backoff attempt count.

    Per VOCABULARY.md §1/§4 status is never colour alone — the ``●``/``o`` dot pairs with
    the state *word* here, so a non-emoji terminal (``ascii_mode``) loses nothing.
    """
    dot = "o" if ascii_mode else "●"
    if health and health.get("code"):
        state = f"DOWN:{health['code']}"
    elif conn_status == "live":
        state = "live"
    elif conn_attempts:
        state = f"reconnecting({conn_attempts})"
    else:
        state = "connecting"
    return f"{dot} monitor: {state}"


def cost_line(flights) -> str:
    """Header cost cell (T3): the summed per-flight ``cost.usd`` with an ``EST`` badge.

    NEVER a bare ``$`` (VOCABULARY.md §3 / COST-1 honesty): provider ``usage`` is not
    threaded through the send seam yet, so every figure the gateway emits is a post-hoc
    ``measure_spend`` estimate — the ``EST`` provenance badge is mandatory."""
    total = sum(float((getattr(v, "cost", None) or {}).get("usd") or 0.0) for v in flights)
    return f"${total:.3f} EST"


def total_tokens(flights) -> int:
    """Sum of every flight's estimated token tick (T4) — the fleet-wide resource total
    the header shows (now non-zero since G0(b) emits ``tokens`` on terminal worker frames)."""
    return sum(int(v.tokens()) for v in flights)


def chat_cell(title: str | None, *, max_len: int = 24) -> str:
    """Status-row cell for the active conversation title (Wave 1B Task 1) — truncated to
    ``max_len`` with an ellipsis; empty string when no conversation is bound yet, so the
    row is byte-identical to before until a conversation exists."""
    t = (title or "").strip()
    if not t:
        return ""
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


def pins_cell(face: str | None, model: str | None, backend: str | None,
              profile: str | None) -> str:
    """Status-row cell for the active conversation's pins (Wave 1B Task 2) —
    ``face: sage · model: gpt-5@openai · profile: council-max``. Empty string when
    nothing is pinned, so unpinned rows are byte-identical to before (and old fake
    chat clients without pin attrs stay compatible)."""
    parts = []
    if face:
        parts.append(f"face: {face}")
    if backend:
        parts.append(f"model: {f'{model}@' if model else ''}{backend}")
    if profile:
        parts.append(f"profile: {profile}")
    return " · ".join(parts)


def switch_ack_line(ack: dict) -> str:
    """One agent-log line for a ``*_switched`` ack (Wave 1B Task 2). A null pin means
    the gateway cleared it back to auto routing (empty value = unpin, per routers/chat)."""
    kind = ack.get("type")
    if kind == "face_switched":
        face = ack.get("face_id")
        return f"face pinned: {face}" if face else "face unpinned (auto routing)"
    if kind == "model_switched":
        model, backend = ack.get("model"), ack.get("backend")
        if not backend:
            return "model unpinned (auto routing)"
        return f"model pinned: {f'{model} @ ' if model else ''}{backend}"
    if kind == "profile_switched":
        profile = ack.get("profile")
        return f"profile pinned: {profile}" if profile else "profile unpinned"
    return f"ack: {ack}"


def header_line(
    flights, health: dict | None, conn_status: str, conn_attempts: int,
    *, ascii_mode: bool = False,
) -> str:
    """The full cockpit header row (T2+T3+T4):
    ``● monitor: live   $0.114 EST   ~4.2k tok`` — health · honest cost · token tick."""
    toks = total_tokens(flights)
    return "   ".join([
        health_line(health, conn_status, conn_attempts, ascii_mode=ascii_mode),
        cost_line(flights),
        f"~{fmt_tokens(toks)} tok",
    ])


def orch_header(v, *, ascii_mode: bool = False) -> str:
    """One orchestration ("manager") line for a flight — the tier ABOVE the workers.

    BoB's fan-out is flat today (ONE orchestrator → N flat workers; hierarchical
    managers that dispatch their own workers are a roadmap item), so there is no
    separate manager-agent frame — this header IS the orchestration view, built from
    the dispatch wave marker (``fleet_start``), the reduce (``fleet_join``), and, for a
    council, the synthesizer (``council_synth``). Always ends with live run/done counts
    so a flight groups its workers even before a wave marker arrives. ``ascii_mode`` swaps
    the VOCABULARY §4 named-flight glyph (``🛰→*``); default is byte-identical to before.
    """
    tag = ("*" if ascii_mode else "🛰") if not v.is_ambient() else "·"
    parts = [f"{tag} {v.flight_id}"]
    f = v.fleet or {}
    if f:
        parts.append(f"{f.get('kind') or 'chat'} wave {f.get('wave', 0)}")
        if f.get("n_workers") is not None:
            parts.append(f"dispatched {f['n_workers']}")
        if f.get("backend"):
            parts.append(str(f["backend"]))
    j = v.join or {}
    if j and j.get("total") is not None:
        failed = j.get("failed") or 0
        parts.append(f"join {j.get('ok')}/{j.get('total')}" + (f" · {failed} failed" if failed else ""))
    synth = (v.council or {}).get("synth") or {}
    if synth:
        parts.append(f"synth[{synth.get('shape') or '?'}] {synth.get('status') or ''}".strip())
    toks = v.tokens()
    if toks:
        parts.append(f"~{fmt_tokens(toks)} tok")
    parts.append(f"[{v.running()} run · {v.done()} done]")
    return "  ".join(parts)


def fleet_lines(flights, *, max_lines: int = 40, ascii_mode: bool = False) -> list[str]:
    """Render the Fleet pane as a two-tier tree: per-flight orchestration header
    (manager/dispatch/join/synth) with the workers — and council seats — indented
    beneath it. Tailed to ``max_lines`` so a 100-agent wave doesn't overflow the pane —
    and when it tails, an explicit ``… +N earlier lines`` marker at the TOP surfaces the
    dropped rows (T6: routing/approvals already mark ``+N more``; the fleet tail was
    silent). The marker sits above because ``fleet_lines`` keeps the *latest* rows.
    """
    out: list[str] = []
    for v in flights:
        out.append(orch_header(v, ascii_mode=ascii_mode))
        for idx in sorted(v.workers):
            w = v.workers[idx]
            role = w.get("role") or "worker"
            out.append(f"    w{idx} {str(w.get('status') or '?'):<8} {role} · {w.get('backend')}")
        seats = (v.council or {}).get("seats") or {}
        for sidx in sorted(seats):
            s = seats[sidx]
            out.append(
                f"    seat{sidx} {str(s.get('status') or '?'):<8} "
                f"{s.get('posture') or 'seat'} · {s.get('backend')} r{s.get('round')}"
            )
    if not out:
        return ["no fleet activity yet"]
    if len(out) > max_lines:
        dropped = len(out) - max_lines
        ell = "..." if ascii_mode else "…"
        return [f"{ell} +{dropped} earlier lines"] + out[-max_lines:]
    return out


def approvals_lines(payload: dict, *, max_items: int = 18, ascii_mode: bool = False,
                    selected: int | None = None) -> list[str]:
    """Shape the ``/approvals?status=pending`` inbox into cockpit lines.

    The TUI is now the ONLY human-reachable approvals surface (the gateway ``/ui`` was
    removed and the KMM app is deferred), so this pane is actionable: ``j``/``k`` move the
    selection (``>`` marker on row ``selected``), ``a``/``d`` start an approve/deny with a
    typed-y confirm in the agent log (Wave 1B Task 4). The always-human Gate is preserved —
    the TUI is a human-authenticated surface. An empty inbox reads as a clean ✓.
    ``ascii_mode`` swaps the VOCABULARY §4 glyphs (``✓→ok``, ``…→...``); a ``selected`` of
    None (no selection tracked, e.g. pure tests) is byte-identical to before.
    """
    if not isinstance(payload, dict) or "items" not in payload:
        return _err_line(payload, "approvals")
    items = payload.get("items") or []
    if not items:
        return [f"no pending approvals {'ok' if ascii_mode else '✓'}"]
    out = [f"{len(items)} pending (j/k select · a approve · d deny · "
           f"while typing use ^↓ ^↑ ^y ^n):", ""]
    for i, a in enumerate(items[:max_items]):
        act = str(a.get("action_type") or "?")[:22]
        conv = str(a.get("conversation_id") or "")[:8]
        mark = ">" if selected == i else "•"
        out.append(f"{mark} {act:<22} conv:{conv or '—'}")
    if len(items) > max_items:
        g_ell = "..." if ascii_mode else "…"
        out.append(f"{g_ell} +{len(items) - max_items} more")
    return out


def _face_map(faces) -> dict | None:
    """``{face_id: face_dict}`` from a ``GET /faces`` payload (a bare list of summaries,
    or a ``{"faces": [...]}``/``{"items": [...]}`` wrapper). ``None`` when the payload is
    unavailable (error/garbled) — callers then fail OPEN (the roster still renders)."""
    items = None
    if isinstance(faces, list):
        items = faces
    elif isinstance(faces, dict) and "error" not in faces:
        items = faces.get("faces") or faces.get("items")
    if not isinstance(items, list):
        return None
    return {str(f.get("id")): f for f in items if isinstance(f, dict) and f.get("id")}


def _is_teammate_face(face: dict | None) -> bool:
    """A face is roster-eligible when marked ``bot: true`` OR carrying a ``simple_slot``
    (Simple-mode faces are already teammate-shaped — mirrors ``Face.is_teammate`` in
    core). A face MISSING from the registry payload keeps its binding (the binding row
    is the bot identity; we can't prove a non-teammate from a missing face)."""
    if face is None:
        return True
    return bool(face.get("bot")) or face.get("simple_slot") is not None


def bot_rows(agents: dict, faces) -> list[dict] | None:
    """The teammate bindings for the Bots pane (Wave 2 Task 3): the ``GET /agents``
    items whose face is teammate-eligible per ``GET /faces`` (defensive filter —
    ``bot: true`` OR ``simple_slot``; a missing/unavailable faces payload fails OPEN).
    ``None`` when the agents payload is bad, so the formatter renders ``unavailable``."""
    if not isinstance(agents, dict) or "items" not in agents:
        return None
    items = [b for b in (agents.get("items") or []) if isinstance(b, dict)]
    fmap = _face_map(faces)
    if fmap is None:
        return items
    return [b for b in items if _is_teammate_face(fmap.get(str(b.get("face_id") or "")))]


def bots_lines(agents: dict, faces, last_seen: dict | None = None,
               *, ascii_mode: bool = False) -> list[str]:
    """Shape ``GET /agents`` + ``GET /faces`` into the Bots pane: a header + one row per
    teammate binding — unread marker, avatar, display_name, slug, and the binding's
    ``updated_at`` (trimmed to the minute) as last-activity when the canonical
    conversation is bound. ``last_seen`` maps slug → watermark timestamp (the state
    file's ``bot_last_seen``); a binding whose activity is NEWER than its watermark
    renders the unread mark (``●`` / ``*`` in ASCII). Row order matches
    :func:`bot_rows`, so the poll loop can zip rows with bindings for the OptionList."""
    rows = bot_rows(agents, faces)
    if rows is None:
        return _err_line(agents, "bots")
    if not rows:
        return ["no bots yet — a teammate appears once its binding is created"]
    seen = last_seen if isinstance(last_seen, dict) else {}
    g_new = "*" if ascii_mode else "●"
    out = [f"{len(rows)} bot{'s' if len(rows) != 1 else ''} (Enter opens chat):"]
    for b in rows:
        slug = str(b.get("slug") or "?")
        name = str(b.get("display_name") or slug)
        avatar = str(b.get("avatar") or "?")
        raw = str(b.get("updated_at") or "") if b.get("conversation_id") else ""
        unread = bool(raw) and raw > str(seen.get(slug) or "")
        row = f"{g_new if unread else ' '} {avatar} {name} ({slug})"
        if raw:
            row += f" · {raw[:16]}"
        out.append(row)
    return out


def _details_summary(details, *, max_len: int = 60) -> str:
    """One-line summary of an approval's ``details`` dict — the first couple of
    ``key=value`` pairs, truncated, so the prompt stays on one log line."""
    if not isinstance(details, dict) or not details:
        return ""
    parts = [f"{k}={str(v)[:24]}" for k, v in list(details.items())[:3]]
    summary = ", ".join(parts)
    if len(details) > 3:
        summary += ", …"
    return summary[:max_len]


def approval_prompt_lines(frame: dict, *, ascii_mode: bool = False) -> list[str]:
    """Agent-log lines for an in-chat ``approval_request`` frame (Wave 1B Task 4): the
    action type + a details summary + how to answer. Answering ``y``/``n`` sends the
    ``approval_response`` frame; Esc dismisses WITHOUT deciding (the approval stays
    pending for the pane)."""
    g_warn = "!" if ascii_mode else "⚠"
    action = str(frame.get("action") or frame.get("action_type") or "?")
    summary = _details_summary(frame.get("details"))
    head = f"{g_warn} approval requested: {action}" + (f" — {summary}" if summary else "")
    return [head, "  answer y to approve, n to deny (Esc dismisses without deciding)"]


def decide_result_line(result: dict) -> str:
    """One agent-log line for a ``POST /approvals/{id}/decide`` response (Wave 1B Task 4):
    the recorded status plus the ``agent_resume`` outcome (the gateway records the decision
    even when resuming the agent fails — that failure must be visible, not silent)."""
    status = str(result.get("status") or "decided")
    resume = result.get("agent_resume")
    line = f"approval {status}"
    if resume == "ok":
        return f"{line} · agent resumed"
    if resume == "failed":
        msg = str(result.get("agent_resume_message") or "")[:60]
        return f"{line} · agent resume FAILED" + (f": {msg}" if msg else "")
    return line
