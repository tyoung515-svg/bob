#!/usr/bin/env python3
"""CPU-only headless demo of the EXISTING BoB slice (FX-READ-01), real binaries.

This drives the real `bob-slice-cli` engine binary over the real S5 NDJSON
protocol on stdin/stdout. Nothing is mocked:

  * the CLI provisions the fixture, the ledger and the scripted upstream;
  * the authority gate, the approval preview, the real bubblewrap executor
    (`fx-read` inside bwrap) and the durable ledger are the shipped ones;
  * every line printed in the summary is parsed from the engine's own output.

What IS scripted: the *client*. This program plays the part of the shell —
it sends the fixed request prefix (hello, session.create, turn.start) and
answers `approval.requested` with `interaction.resolve`, exactly as
`shell/src/App.tsx` does. The model/upstream call itself is the fixed
FX-READ-01 scripted call (`fx-read ./hello.txt`) that the engine plays after
`turn.start`; there is no LLM, no network and no arbitrary command.

Scenarios (each gets its own fresh data root so ledgers never mix):

  allow            allow_once            -> tool.result succeeded
  deny             deny with a reason    -> tool.result denied, reason verbatim
  mismatch-retry   corrupted args_digest -> approval_mismatch, request stays
                   pending; the SAME request id is then retried with the real
                   digest -> tool.result succeeded

Usage:
  python3 scripts/linux/headless-demo.py                # fresh user-cache run dir
  python3 scripts/linux/headless-demo.py --run-dir DIR
  python3 scripts/linux/headless-demo.py --scenario allow

Exit status: 0 only if every selected scenario produced its expected real
frames/responses/ledger. Non-zero on any deviation (an honest failure, never
a skipped check).
"""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HELLO = {"id": "r1", "method": "hello", "params": {"version": {"major": 2, "minor": 0}}}
SESSION = {"id": "r2", "method": "session.create", "params": {}}
TURN = {"id": "r3", "method": "turn.start", "params": {"model": "slice-model"}}
DENY_REASON = "demo: deny with reason (deny-once, nothing executed)"

SCENARIOS = ("allow", "deny", "mismatch-retry")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_run_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bob-preview" / "runs"
    return base / f"{stamp}-headless-{uuid.uuid4().hex[:6]}"


def corrupt_digest(digest: str) -> str:
    """Flip the last hex nibble, never a no-op (same trick as the shell card)."""
    last = digest[-1]
    return digest[:-1] + ("1" if last == "0" else "0")


class Engine:
    """One real bob-slice-cli child process + its stdout/stderr line streams."""

    def __init__(self, cli: Path, fx_read: Path, data_root: Path, scenario_dir: Path):
        self.scenario_dir = scenario_dir
        self.data_root = data_root
        self.responses = []   # request/response lines (no "event")
        self.frames = []      # event frames (seq gap-free from 1)
        self.stderr = []
        self.lines = []       # every stdout line, verbatim, in arrival order
        data_root.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [str(cli), "--data-root", str(data_root), "--fx-read", str(fx_read)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)

    def _pump_err(self):
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip("\n"))

    def send(self, obj):
        line = json.dumps(obj, separators=(",", ":"))
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def next_line(self, timeout=30.0):
        try:
            line = self.q.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no engine output within {timeout}s (stderr={self.stderr[-3:]})")
        if line is None:
            raise AssertionError(f"engine stdout closed (exit={self.proc.poll()}, stderr={self.stderr[-3:]})")
        self.lines.append(line)
        obj = json.loads(line)
        if "event" in obj:
            self.frames.append(obj)
        else:
            self.responses.append(obj)
        return obj

    def wait_response(self, rid, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.next_line(max(0.1, deadline - time.monotonic()))
            if obj.get("id") == rid and "event" not in obj:
                return obj
        raise AssertionError(f"no response for {rid}")

    def wait_frame(self, event, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = self.next_line(max(0.1, deadline - time.monotonic()))
            if obj.get("event") == event:
                return obj
        raise AssertionError(f"no {event} frame")

    def drain_until_exit(self, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                obj = self.next_line(max(0.1, deadline - time.monotonic()))
            except AssertionError:
                # stdout closed (or the window elapsed). The exit status below
                # is the assertion that matters; `poll()` may still be None for
                # the instant between the write end closing and the engine being
                # reaped, which used to surface as a spurious scenario failure.
                break
            del obj
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()

    def close_stdin(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass


def ledger_files(data_root: Path):
    return sorted((data_root / "ledgers").glob("*/segments/*.audit.jsonl"))


def ledger_records(data_root: Path):
    recs = []
    for path in ledger_files(data_root):
        for line in path.read_text().splitlines():
            if line.strip():
                recs.append(json.loads(line))
    return recs


def write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def run_scenario(name: str, cli: Path, fx_read: Path, run_dir: Path) -> dict:
    sdir = run_dir / name
    data_root = sdir / "data"
    if data_root.exists():
        shutil.rmtree(data_root)
    eng = Engine(cli, fx_read, data_root, sdir)
    checks = []
    notes = []

    def check(label, ok, detail=""):
        checks.append({"check": label, "ok": bool(ok), "detail": str(detail)})
        if not ok:
            raise AssertionError(f"{label}: {detail}")

    try:
        eng.send(HELLO)
        hello = eng.wait_response("r1")
        check("hello accepted", "result" in hello, hello)
        notes.append(f"schema_hash={hello['result'].get('schema_hash')}")

        eng.send(SESSION)
        sess = eng.wait_response("r2")
        check("session.create accepted", "result" in sess, sess)

        eng.send(TURN)
        # The `turn.start` *response* is only offered when the turn ends; the
        # lifecycle starts with the frames (S5: ledger-before-offer).
        approval = eng.wait_frame("approval.requested")
        preview = approval["payload"]
        check("approval preview carries call_id", bool(preview.get("call_id")), preview.get("call_id"))
        check("approval preview carries decision_id", bool(preview.get("decision_id")), preview.get("decision_id"))
        check("args_digest is sha256", str(preview.get("args_digest", "")).startswith("sha256:"), preview.get("args_digest"))
        check("effect_class is read", preview.get("effect_class") == "read", preview.get("effect_class"))
        check("action summary is engine-derived", "fx-read ./hello.txt" in (preview.get("action_summary") or "")
              or "hello.txt" in (preview.get("action_summary") or ""), preview.get("action_summary"))

        def resolve(rid, digest, response):
            eng.send({"id": rid, "method": "interaction.resolve",
                      "params": {"call_id": preview["call_id"],
                                 "decision_id": preview["decision_id"],
                                 "args_digest": digest, "response": response}})

        corrected = False
        if name == "mismatch-retry":
            resolve("r4", corrupt_digest(preview["args_digest"]), "allow_once")
            err = eng.wait_response("r4")
            check("corrupted digest -> approval_mismatch error",
                  err.get("error", {}).get("code") == "approval_mismatch", err)
            check("mismatch appended no frame",
                  not any(f.get("event") in ("approval.resolved", "tool.result") for f in eng.frames),
                  [f.get("event") for f in eng.frames])
            corrected = True
            resolve("r4", preview["args_digest"], "allow_once")
        elif name == "allow":
            resolve("r4", preview["args_digest"], "allow_once")
        elif name == "deny":
            resolve("r4", preview["args_digest"], {"deny": {"reason": DENY_REASON}})
        else:
            raise AssertionError(f"unknown scenario {name}")

        resolved = eng.wait_frame("approval.resolved")
        result = eng.wait_frame("tool.result")
        completed = eng.wait_frame("turn.completed")
        eng.close_stdin()  # transport EOF, never a turn cancel (S5 §1)
        exit_code = eng.drain_until_exit()

        expected = {"allow": ("succeeded", "succeeded"), "mismatch-retry": ("succeeded", "succeeded"),
                    "deny": ("denied", "failed")}[name]
        check("tool.result status", result["payload"].get("status") == expected[0],
              f"{result['payload'].get('status')} != {expected[0]}")
        check("turn.completed status", completed["payload"].get("status") == expected[1],
              f"{completed['payload'].get('status')} != {expected[1]}")
        check("engine exit code 0", exit_code == 0, exit_code)

        if name == "deny":
            check("denial reason verbatim on the wire", resolved["payload"].get("reason") == DENY_REASON,
                  resolved["payload"].get("reason"))
            check("no tool.started after denial", not any(f.get("event") == "tool.started" for f in eng.frames),
                  [f.get("event") for f in eng.frames])
            check("no executor evidence for a denial",
                  result["payload"].get("executor_evidence") in (None, {}), result["payload"].get("executor_evidence"))
        if name in ("allow", "mismatch-retry"):
            out = result["payload"].get("output")
            text = out if isinstance(out, str) else json.dumps(out)
            check("fixture bytes returned", "bob-slice-fixture" in text, text[:120])
            ev = result["payload"].get("executor_evidence") or {}
            ev_text = json.dumps(ev)
            check("real bubblewrap backend reported", "linux_bubblewrap" in ev_text, ev_text[:200])
            check("tool.started witness present", any(f.get("event") == "tool.started" for f in eng.frames),
                  [f.get("event") for f in eng.frames])
        if corrected:
            check("corrected retry finished the same turn", completed["payload"].get("status") == "succeeded",
                  completed["payload"])

        # seq must be gap-free from 1 (S5 §2)
        seqs = [f["seq"] for f in eng.frames]
        check("frame seq gap-free from 1", seqs == list(range(1, len(seqs) + 1)), seqs)

        recs = ledger_records(data_root)
        kinds = [r.get("kind") for r in recs]
        check("durable ledger written", bool(recs), [str(p) for p in ledger_files(data_root)])
        for needed in ("lineage", "turn.start", "approval.requested", "approval.resolved", "tool.result", "turn.end"):
            check(f"ledger has {needed}", needed in kinds, kinds)
        if name == "deny":
            check("ledger has no tool.started", "tool.started" not in kinds, kinds)
        else:
            check("ledger has tool.started", "tool.started" in kinds, kinds)

        summary = {
            "scenario": name,
            "live_run": True,
            "client": "scripts/linux/headless-demo.py (scripted request prefix; plays the shell)",
            "upstream": "engine-scripted fixed FX-READ-01 call (fx-read ./hello.txt); no model, no network",
            "engine_binary": str(cli),
            "engine_binary_sha256": sha256_of(cli),
            "fx_read_binary": str(fx_read),
            "fx_read_binary_sha256": sha256_of(fx_read),
            "data_root": str(data_root),
            "ledger_files": [str(p) for p in ledger_files(data_root)],
            "ledger_records": len(recs),
            "ledger_kinds": kinds,
            "frames": len(eng.frames),
            "responses": len(eng.responses),
            "frame_events": [f.get("event") for f in eng.frames],
            "tool_result_status": result["payload"].get("status"),
            "turn_status": completed["payload"].get("status"),
            "engine_exit": exit_code,
            "checks": checks,
            "checks_passed": sum(1 for c in checks if c["ok"]),
            "checks_total": len(checks),
            "notes": notes,
        }
    except Exception as exc:  # honest failure: keep the partial evidence
        summary = {
            "scenario": name,
            "live_run": True,
            "error": f"{type(exc).__name__}: {exc}",
            "data_root": str(data_root),
            "checks": checks,
            "checks_passed": sum(1 for c in checks if c["ok"]),
            "checks_total": len(checks),
            "frames": [f.get("event") for f in eng.frames],
            "responses": eng.responses,
            "stderr": eng.stderr,
        }
        eng.close_stdin()
        try:
            eng.proc.kill()
        except Exception:
            pass
        write_jsonl(sdir / "stdout.ndjson", [json.loads(l) for l in eng.lines])
        (sdir / "stderr.log").write_text("\n".join(eng.stderr) + "\n")
        (sdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary

    write_jsonl(sdir / "stdout.ndjson", [json.loads(l) for l in eng.lines])
    (sdir / "stderr.log").write_text("\n".join(eng.stderr) + "\n")
    (sdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_receipt(run_dir: Path, summaries, started: str, elapsed: float) -> Path:
    ok = all("error" not in s and s.get("checks_passed") == s.get("checks_total") for s in summaries)
    lines = [
        "# BoB slice — headless demo receipt (live run, real binaries)",
        "",
        f"- run dir: `{run_dir}`",
        f"- generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime())} (started {started}, {elapsed:.1f}s)",
        f"- result: **{'PASS' if ok else 'FAIL'}**",
        "",
        "Every row below was parsed from a real `bob-slice-cli` process driving the",
        "real bubblewrap executor and writing its own durable ledger. The *client*",
        "is scripted (this demo plays the shell); the upstream call is the engine's",
        "fixed FX-READ-01 script. No LLM, no network, no arbitrary command.",
        "",
        "| scenario | tool.result | turn.completed | engine exit | frames | ledger records | checks |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            "| {s} | {r} | {t} | {e} | {f} | {l} | {p}/{n} |".format(
                s=s.get("scenario"), r=s.get("tool_result_status", "-"), t=s.get("turn_status", "-"),
                e=s.get("engine_exit", "-"), f=len(s.get("frame_events") or s.get("frames") or []),
                l=s.get("ledger_records", "-"), p=s.get("checks_passed"), n=s.get("checks_total")))
    lines += ["", "## Paths per scenario", ""]
    for s in summaries:
        lines.append(f"- **{s.get('scenario')}**: data root `{s.get('data_root')}`"
                     + (f" — error: {s['error']}" if "error" in s else ""))
        for lf in s.get("ledger_files") or []:
            lines.append(f"  - ledger: `{lf}`")
    lines += ["", "## How to inspect", "",
              "```sh",
              f"ls -R {run_dir}",
              f"cat {run_dir}/<scenario>/stdout.ndjson      # every protocol line, verbatim",
              f"cat {run_dir}/<scenario>/stderr.log         # engine diagnostics",
              f"cat {run_dir}/<scenario>/data/ledgers/*/segments/*.audit.jsonl  # durable ledger",
              "```", ""]
    path = run_dir / "RECEIPT.md"
    path.write_text("\n".join(lines))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, default=None, help="artifact dir (default: fresh user-cache run dir)")
    ap.add_argument("--repo", type=Path, default=None, help="repo root (default: two levels above this script)")
    ap.add_argument("--cli", type=Path, default=None, help="bob-slice-cli binary (default: <repo>/target/debug)")
    ap.add_argument("--fx-read", type=Path, default=None, help="fx-read binary (default: <repo>/target/debug)")
    ap.add_argument("--scenario", action="append", choices=SCENARIOS, help="repeatable; default: all")
    args = ap.parse_args()

    repo = (args.repo or repo_root()).resolve()
    cli = (args.cli or repo / "target" / "debug" / "bob-slice-cli").resolve()
    fx = (args.fx_read or repo / "target" / "debug" / "fx-read").resolve()
    run_dir = (args.run_dir or default_run_dir()).expanduser().resolve()

    missing = [str(p) for p in (cli, fx) if not p.is_file()]
    if missing:
        print("headless-demo: missing engine binary: " + ", ".join(missing), file=sys.stderr)
        print("headless-demo: build them first with `cargo build` at the repo root "
              "(or use scripts/linux/run-preview.sh --build-only)", file=sys.stderr)
        return 2
    bwrap = shutil.which("bwrap")
    if not bwrap:
        print("headless-demo: bwrap (bubblewrap) is required: the real executor cannot run "
              "without it and this demo does not fall back to an unsandboxed path", file=sys.stderr)
        return 2

    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    t0 = time.monotonic()
    summaries = [run_scenario(name, cli, fx, run_dir) for name in (args.scenario or list(SCENARIOS))]
    elapsed = time.monotonic() - t0
    receipt = write_receipt(run_dir, summaries, started, elapsed)
    (run_dir / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")

    ok = True
    for s in summaries:
        good = "error" not in s and s.get("checks_passed") == s.get("checks_total")
        ok = ok and good
        if "error" in s:
            print(f"FAIL {s['scenario']}: {s['error']}")
        else:
            print("PASS {s}: tool.result={r} turn={t} exit={e} frames={f} ledger={l} checks={p}/{n}".format(
                s=s["scenario"], r=s["tool_result_status"], t=s["turn_status"], e=s["engine_exit"],
                f=len(s["frame_events"]), l=s["ledger_records"], p=s["checks_passed"], n=s["checks_total"]))
    print(f"artifacts: {run_dir}")
    print(f"receipt:   {receipt}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
