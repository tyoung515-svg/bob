#!/usr/bin/env python3
"""Stdio protocol double for CONTRACT section 3 - TRANSPORT_SIMULATED ONLY.

Implements the portal broker's JSON-lines protocol against an in-memory fixture
so the Windows transport can be exercised end to end without the server, without
a token and without any model call. It is never installed on the server.

Frames:
  request {"id":<uint>,"op":"<name>","args":{...}}
  reply   {"id":<uint>,"ok":true,"value":{...}} | {"id":<uint>,"ok":false,"error":{"code","detail"}}
  event   {"event":"<name>","data":{...}}   (no id)
"""
import json
import sys
import time

try:  # Windows redirected stdout is cp1252 by default; the wire is UTF-8 only.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except Exception:  # noqa: BLE001 - fixture only
    pass

BOUND_SUB = "dad"

FACES = [
    {"id": "assistant", "name": "Assistant"},
    {"id": "researcher", "name": "Researcher"},
    {"id": "builder-bob", "name": "Builder Bob"},
]
MODELS = [
    {"id": "deepseek_v4_flash", "backend": "deepseek", "model": "deepseek-v4-flash", "available": True, "reason": None},
    {"id": "planner_gpt56_sol", "backend": "openai", "model": "gpt-5.6-sol", "available": False, "reason": "no credential on this deployment"},
]
BACKENDS = [
    {"id": "deepseek", "kind": "openai_compatible", "available": True, "unavailable_reason": None},
    {"id": "openai", "kind": "openai_compatible", "available": False, "unavailable_reason": "no credential on this deployment"},
]
GAPS = [
    "no build_request ingress at gateway",
    "no authenticated artifact list/read/download API",
    "no run list/DAG/parent-branch API",
    "no structured tool event stream inside normal chat",
    "no idempotent send / resend handshake",
    "approvals.decide refused until the staged gateway guard is activated",
    "local-runtime deployment is not delivered by this program",
]

STATE = {
    "conversations": {},
    "messages": {},
    "approvals": {},
    "projects": {},
    "runs": {},
    "seq": 0,
}


def now():
    return "2026-10-24T12:00:00+07:00"


def next_id(prefix):
    STATE["seq"] += 1
    return "%s-%04d" % (prefix, STATE["seq"])


def out(frame):
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(rid, value):
    out({"id": rid, "ok": True, "value": value})


def fail(rid, code, detail):
    out({"id": rid, "ok": False, "error": {"code": code, "detail": detail}})


def event(name, data):
    out({"event": name, "data": data})


def conversation(cid):
    return STATE["conversations"][cid]


def new_conversation(title, face_id=None, project_id=None):
    cid = next_id("conv")
    row = {
        "id": cid,
        "title": title,
        "face_id": face_id or "assistant",
        "backend": None,
        "model": None,
        "project_id": project_id,
        "user_id": BOUND_SUB,
        "created_at": now(),
        "updated_at": now(),
    }
    STATE["conversations"][cid] = row
    STATE["messages"][cid] = [
        {"id": next_id("msg"), "role": "user", "content": "fixture opener", "created_at": now(), "metadata": {"source": "fixture"}},
        {"id": next_id("msg"), "role": "assistant", "content": "fixture reply", "created_at": now(), "metadata": {"source": "fixture"}},
    ]
    return row


def seed():
    new_conversation("PORTAL-SIM conversation one")
    new_conversation("PORTAL-SIM conversation two")
    pid = next_id("proj")
    STATE["projects"][pid] = {
        "id": pid,
        "name": "PORTAL-SIM project",
        "description": "fixture project",
        "instructions": "be brief",
        "default_face_id": "assistant",
        "default_backend": None,
        "conversation_count": 0,
        "updated_at": now(),
    }
    aid = "appr-dad-1"
    STATE["approvals"][aid] = {
        "id": aid,
        "conversation_id": list(STATE["conversations"])[0],
        "action_type": "file_write",
        "details": {"path": "/srv/bob/owner/fixture.txt"},
        "status": "pending",
        "created_at": now(),
        "user_id": BOUND_SUB,
    }
    foreign = "appr-foreign-1"
    STATE["approvals"][foreign] = {
        "id": foreign,
        "conversation_id": list(STATE["conversations"])[0],
        "action_type": "file_write",
        "details": {"path": "/srv/bob/shared/fixture.txt"},
        "status": "pending",
        "created_at": now(),
        "user_id": "luna",
    }


def handle_chat_send(rid, args):
    cid = args.get("conversation_id") or ""
    content = args.get("content") or ""
    if cid not in STATE["conversations"]:
        return fail(rid, "conversation_not_found", "no such conversation for this identity")
    if not content.strip():
        return fail(rid, "bad_request", "content required")
    request_id = next_id("req")
    pins = {
        "conversation_id": cid,
        "face_id": args.get("face_id") or conversation(cid)["face_id"],
        "backend": args.get("backend") or "deepseek",
        "model": args.get("model") or "deepseek-v4-flash",
        "profile": args.get("profile"),
        "locale": args.get("locale") or "en",
    }
    reply(rid, {"request_id": request_id, "accepted": True})
    event("pins", pins)
    text = "simulated reply to: " + content[:80]
    for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
        event("chunk", {"request_id": request_id, "content": piece, "model": pins["model"], "backend": pins["backend"]})
    message_id = next_id("msg")
    STATE["messages"][cid].append(
        {"id": message_id, "role": "assistant", "content": text, "created_at": now(), "metadata": {"request_id": request_id}}
    )
    event(
        "complete",
        {"request_id": request_id, "message_id": message_id, "tokens_in": 11, "tokens_out": 22, "elapsed_ms": 34},
    )


def handle(rid, op, args):
    if op == "ping":
        return reply(rid, {"ts": now()})
    if op == "slow":
        # Fixture-only (never a broker op): answers after the caller's deadline
        # so the transport test can prove a request timeout abandons that request
        # without killing the shared link, and that the late reply is dropped.
        time.sleep(1.5)
        return reply(rid, {"slow": True})
    if op == "crash":
        sys.stdout.flush()
        sys.exit(3)
    if op == "hello":
        return reply(
            rid,
            {
                "identity": {
                    "sub": BOUND_SUB,
                    "gateway": "127.0.0.1:7836",
                    "faces": FACES,
                    "may_touch": ["/srv/bob/owner", "/srv/bob/shared"],
                    "may_not_touch": ["/srv/bob/other"],
                    "expires_at": "2026-11-24T00:00:00Z",
                },
                "server": {"boot": "2026-10-24T01:57:51+07", "version": "portal-double-1"},
                "gaps": GAPS,
            },
        )
    if op == "catalog":
        return reply(
            rid,
            {
                "faces": FACES,
                "models": MODELS,
                "backends": BACKENDS,
                "capabilities": {"faces": len(FACES), "backends": len(BACKENDS), "actions": [], "warnings": ["fixture catalog - TRANSPORT_SIMULATED"]},
            },
        )
    if op == "conversations.list":
        limit = int(args.get("limit") or 30)
        offset = int(args.get("offset") or 0)
        rows = [c for c in STATE["conversations"].values() if c["user_id"] == BOUND_SUB]
        return reply(rid, {"items": rows[offset : offset + limit], "limit": limit, "offset": offset, "excluded": 0})
    if op == "conversations.create":
        row = new_conversation(args.get("title") or "untitled", args.get("face_id"), args.get("project_id"))
        return reply(rid, {"conversation": row})
    if op == "conversations.get":
        cid = args.get("id") or ""
        if cid not in STATE["conversations"]:
            return fail(rid, "conversation_not_found", "no such conversation for this identity")
        return reply(rid, {"conversation": conversation(cid)})
    if op == "conversations.rename":
        cid = args.get("id") or ""
        if cid not in STATE["conversations"]:
            return fail(rid, "conversation_not_found", "no such conversation for this identity")
        conversation(cid)["title"] = args.get("title") or conversation(cid)["title"]
        conversation(cid)["updated_at"] = now()
        return reply(rid, {"conversation": conversation(cid)})
    if op == "conversations.assign_project":
        cid = args.get("id") or ""
        if cid not in STATE["conversations"]:
            return fail(rid, "conversation_not_found", "no such conversation for this identity")
        conversation(cid)["project_id"] = args.get("project_id")
        return reply(rid, {"conversation": conversation(cid)})
    if op == "messages.page":
        cid = args.get("id") or ""
        if cid not in STATE["messages"]:
            return fail(rid, "conversation_not_found", "no such conversation for this identity")
        limit = int(args.get("limit") or 50)
        rows = STATE["messages"][cid]
        return reply(rid, {"items": rows[-limit:], "has_more": len(rows) > limit})
    if op == "chat.send":
        return handle_chat_send(rid, args)
    if op == "chat.stop":
        return reply(rid, {"stopped": True, "code": "stopped"})
    if op == "chat.pins":
        cid = args.get("conversation_id") or ""
        if cid not in STATE["conversations"]:
            return fail(rid, "conversation_not_found", "no such conversation for this identity")
        row = conversation(cid)
        return reply(rid, {"face_id": row["face_id"], "backend": row["backend"], "model": row["model"], "profile": None, "locale": "en"})
    if op == "approvals.list":
        status = args.get("status") or "pending"
        limit = int(args.get("limit") or 30)
        rows = [a for a in STATE["approvals"].values() if a["user_id"] == BOUND_SUB and a["status"] == status]
        return reply(rid, {"items": rows[:limit], "excluded": sum(1 for a in STATE["approvals"].values() if a["user_id"] != BOUND_SUB)})
    if op == "approvals.get":
        aid = args.get("approval_id") or ""
        row = STATE["approvals"].get(aid)
        if row is None:
            return fail(rid, "approval_not_found", "no such approval for this identity")
        if row["user_id"] != BOUND_SUB:
            return fail(rid, "approval_foreign", "approval belongs to another owner")
        return reply(rid, {"approval": row})
    if op == "approvals.decide":
        aid = args.get("approval_id") or ""
        row = STATE["approvals"].get(aid)
        if row is None:
            return fail(rid, "approval_not_found", "no such approval for this identity")
        if row["user_id"] != BOUND_SUB:
            return fail(rid, "approval_foreign", "approval belongs to another owner")
        if row["status"] != "pending":
            return fail(rid, "approval_stale", "approval is no longer pending")
        row["status"] = "approved" if args.get("decision") == "approve" else "rejected"
        return reply(rid, {"decision": "recorded", "agent_resume": "ok", "approval": row})
    if op == "projects.list":
        return reply(rid, {"items": list(STATE["projects"].values())})
    if op == "projects.create":
        pid = next_id("proj")
        STATE["projects"][pid] = {
            "id": pid,
            "name": args.get("name") or "untitled",
            "description": args.get("description") or "",
            "instructions": args.get("instructions") or "",
            "default_face_id": args.get("default_face_id"),
            "default_backend": args.get("default_backend"),
            "conversation_count": 0,
            "updated_at": now(),
        }
        return reply(rid, {"project": STATE["projects"][pid]})
    if op == "projects.update":
        pid = args.get("id") or ""
        if pid not in STATE["projects"]:
            return fail(rid, "project_not_found", "no such project")
        for key in ("name", "description", "instructions", "default_face_id", "default_backend"):
            if key in args:
                STATE["projects"][pid][key] = args[key]
        STATE["projects"][pid]["updated_at"] = now()
        return reply(rid, {"project": STATE["projects"][pid]})
    if op == "projects.delete":
        pid = args.get("id") or ""
        if pid not in STATE["projects"]:
            return fail(rid, "project_not_found", "no such project")
        del STATE["projects"][pid]
        return reply(rid, {"deleted": True})
    if op == "memory.status":
        return reply(
            rid,
            {
                "authority": "server",
                "paragraphs": 234,
                "attributed_events": 32,
                "derived_facts": 7,
                "excluded_events": 881,
                "complete_corpus": False,
                "coverage_note": "fixture estimate; snapshot counts are not a completeness claim",
            },
        )
    if op == "memory.search":
        query = args.get("query") or ""
        return reply(
            rid,
            {
                "items": [
                    {
                        "source_id": "family-memory#1",
                        "owner": BOUND_SUB,
                        "kind": "fact",
                        "provenance": "family_memory_paragraph",
                        "excerpt": "fixture excerpt matching %r" % query,
                        "truncated": False,
                    }
                ],
                "coverage": {"complete_corpus": False, "excluded_events": 881},
            },
        )
    if op == "memory.read":
        return reply(
            rid,
            {
                "source_id": args.get("source_id") or "",
                "offset": int(args.get("offset") or 0),
                "content": "fixture memory body for %s" % (args.get("source_id") or ""),
                "truncated": False,
            },
        )
    if op == "memory.save":
        return reply(rid, {"saved": True, "category": args.get("category") or "note"})
    if op == "runs.start":
        run_id = next_id("run")
        STATE["runs"][run_id] = {
            "id": run_id,
            "status": "running",
            "team": args.get("team"),
            "task": args.get("task"),
            "max_model_calls": int(args.get("max_model_calls") or 4),
            "max_seconds": int(args.get("max_seconds") or 900),
            "independently_verified": False,
            "receipts": [
                {"role": "lead", "model_face": "assistant", "elapsed_seconds": 3, "output": "fixture"},
                {"role": "reviewer", "model_face": "reviewer", "elapsed_seconds": 5, "output": "fixture"},
            ],
        }
        return reply(rid, {"run": STATE["runs"][run_id]})
    if op == "runs.get":
        run_id = args.get("run_id") or ""
        if run_id not in STATE["runs"]:
            return fail(rid, "run_not_found", "no such run for this owner")
        return reply(rid, {"run": STATE["runs"][run_id]})
    if op == "runs.cancel":
        run_id = args.get("run_id") or ""
        if run_id not in STATE["runs"]:
            return fail(rid, "run_not_found", "no such run for this owner")
        STATE["runs"][run_id]["status"] = "cancelled"
        return reply(rid, {"run": STATE["runs"][run_id]})
    if op == "runs.followup":
        run_id = args.get("run_id") or ""
        if run_id not in STATE["runs"]:
            return fail(rid, "run_not_found", "no such run for this owner")
        return reply(rid, {"run": STATE["runs"][run_id]})
    return fail(rid, "protocol_invalid", "unsupported op %r in the fixture double" % op)


def main():
    seed()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except Exception as exc:  # noqa: BLE001 - fixture only
            out({"id": 0, "ok": False, "error": {"code": "protocol_invalid", "detail": str(exc)}})
            continue
        rid = frame.get("id")
        op = frame.get("op")
        args = frame.get("args") or {}
        if not isinstance(rid, int) or not isinstance(op, str):
            out({"id": 0, "ok": False, "error": {"code": "protocol_invalid", "detail": "id must be a uint and op a string"}})
            continue
        handle(rid, op, args)


if __name__ == "__main__":
    main()
