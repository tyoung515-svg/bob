import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  encodeRequest,
  parseLine,
  type Frame,
  type Method,
  type RequestParamsMap,
  type ResolveResponse,
} from "./protocol";
import { asText } from "./text";
import {
  onExit,
  onLine,
  onStderr,
  sliceSend,
  sliceShutdown,
  sliceStart,
} from "./client";
import { ApprovalCard } from "./ApprovalCard";
import { FrameRow } from "./FrameRow";
import { initial, reducer } from "./reducer";

// S10 §2 local, disposable reducer domains live in `reducer.ts` (E10 moved
// them out of this file so the JS reducer harness can drive the REAL reducer
// from the committed wire transcripts — no behaviour change). Everything
// rendered derives from protocol frames/responses only; no local clock,
// timeout or countdown exists (C-2), and no mutating control stays enabled
// after the request closes.

let reqSeq = 0;
function nextReqId(prefix: string): string {
  reqSeq += 1;
  return `${prefix}${reqSeq}`;
}

function corruptDigest(d: string): string {
  const last = d.slice(-1);
  return d.slice(0, -1) + (last === "0" ? "1" : "0");
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initial);
  const [tamperEcho, setTamperEcho] = useState(false);
  const started = useRef(false);

  // One request line, encoded by the generated client (A-10): the method
  // selects the params type, so a request cannot be hand-assembled into a
  // shape the engine does not accept.
  const send = <M extends Method,>(id: string, method: M, params: RequestParamsMap[M]) => {
    // fire-and-forget: failures surface as engine error responses/exit
    void sliceSend(encodeRequest(id, method, params));
  };

  const start = async () => {
    if (started.current) return;
    started.current = true;
    try {
      const root = await sliceStart();
      dispatch({ t: "spawned", dataRoot: root });
      send("r1", "hello", { version: { major: 2 } });
    } catch (e) {
      dispatch({ t: "notice", text: `slice_start failed: ${String(e)}` });
      started.current = false;
    }
  };

  useEffect(() => {
    const unlistenLine = onLine((raw) => {
      const parsed = parseLine(raw);
      if (!parsed) return;
      if (parsed.kind === "response") {
        const r = parsed.response;
        dispatch({ t: "response", response: r });
        // Drive the fixed request sequence on results only.
        if ("result" in r && !("error" in r)) {
          if (r.id === "r1") send("r2", "session.create", {});
          else if (r.id === "r2") send("r3", "turn.start", { model: "slice-model" });
        }
        if ("error" in r && r.error && r.id !== null && r.id.startsWith("r4")) {
          // approval_mismatch / approval_closed render verbatim; the
          // request stays pending (controls stay enabled for mismatch).
          dispatch({ t: "notice", text: `${r.error.code}: ${r.error.message}` });
        }
        return;
      }
      const f = parsed.frame;
      // Ack BEFORE the approval prompt renders (S10 ordering: offer only
      // after ack; approval.requested must be acked before the prompt).
      send(nextReqId("ack-"), "event.ack", { through_seq: f.seq });
      dispatch({ t: "frame", frame: f });
    });
    const unlistenErr = onStderr((text) => dispatch({ t: "stderr", text }));
    const unlistenExit = onExit((code) => dispatch({ t: "exit", code }));
    return () => {
      void unlistenLine.then((f) => f());
      void unlistenErr.then((f) => f());
      void unlistenExit.then((f) => f());
    };
  }, []);

  const lastToolResult = useMemo(
    () => [...state.rows].reverse().find((r) => r.frame.event === "tool.result")?.frame ?? null,
    [state.rows],
  );
  const lastResolved = useMemo(
    () => [...state.rows].reverse().find((r) => r.frame.event === "approval.resolved")?.frame ?? null,
    [state.rows],
  );

  const answer = (allow: boolean, reason: string) => {
    const p = state.pending;
    if (!p) return;
    const digest = tamperEcho ? corruptDigest(p.args_digest) : p.args_digest;
    const response: ResolveResponse = allow
      ? "allow_once"
      : reason.trim().length > 0
        ? { deny: { reason } }
        : { deny: {} };
    send("r4", "interaction.resolve", {
      call_id: p.call_id,
      decision_id: p.decision_id,
      args_digest: digest,
      response,
    });
  };

  return (
    <div className="app">
      <header className="banner">
        <h1>BoB slice — one call: FX-READ-01</h1>
        <p className="platform-posture">Experimental slice preview. Linux tier-1 accessibility acceptance pending.
          {" "}Windows: platform_deferred. Isolation facts are reported per call below; this is not a platform certification.</p>
        <p className="conn" data-conn={state.conn}>
          connection: {state.conn}
          {state.dataRoot ? ` · data root: ${state.dataRoot}` : ""}
          {state.exitCode !== null ? ` · engine exit ${state.exitCode}` : ""}
        </p>
        {state.conn === "idle" && (
          <button onClick={() => void start()}>Open session and begin the turn</button>
        )}
      </header>

      <div role="status" aria-live="polite" aria-atomic="true" className="livescreen">
        {state.toolResultStatus
          ? `tool.result: ${state.toolResultStatus}.${state.toolResultStatus === "indeterminate" ? " The outcome is unknown. There is no retry control." : ""}${state.turnStatus ? ` turn.completed: ${state.turnStatus}.` : ""}`
          : state.pending
            ? `approval.requested: ${state.pending.action_summary}${state.requestClosed ? ". This request is closed." : ". Response controls are available after the lifecycle."}`
            : state.rows.length === 0
              ? "waiting for protocol events"
              : `frame ${state.rows[state.rows.length - 1].frame.event} (seq ${state.rows[state.rows.length - 1].frame.seq})`}
        {state.notices.length > 0 ? ` ${state.notices[state.notices.length - 1]}` : ""}
      </div>

      {state.notices.length > 0 && (
        <section className="notices" aria-labelledby="notices-h">
          <h2 id="notices-h">Wire responses rendered verbatim</h2>
          {state.notices.map((n, i) => (
            <p key={i} className="notice">
              {n}
            </p>
          ))}
        </section>
      )}

      <main>
        <section className="lifecycle" aria-labelledby="lifecycle-h">
          <h2 id="lifecycle-h">Lifecycle (one row per protocol frame)</h2>
          {state.rows.length === 0 && <p>no frames yet</p>}
          <ul>
            {state.rows.map((r) => (
              <FrameRow key={r.frame.seq} row={r} />
            ))}
          </ul>
        </section>

        {state.pending && (
          <ApprovalCard
            preview={state.pending}
            closed={state.requestClosed}
            resolution={lastResolved ? lastResolved.payload : null}
            tamperEcho={tamperEcho}
            onTamperEchoChange={setTamperEcho}
            onAllow={() => answer(true, "")}
            onDeny={(reason) => answer(false, reason)}
          />
        )}

        {lastToolResult && (
          <section className="result" aria-labelledby="result-h">
            <h2 id="result-h">Tool outcome</h2>
            <p className="status" data-status={state.toolResultStatus}>
              {state.toolResultStatus}
            </p>
            <ToolResultBody frame={lastToolResult} />
            {lastToolResult.event === "tool.result" && lastToolResult.payload.executor_evidence && (
              <>
                <h3>Executor evidence</h3>
                <pre className="output"><code>{asText(lastToolResult.payload.executor_evidence)}</code></pre>
              </>
            )}
          </section>
        )}

        {state.turnStatus && (
          <section className="turnend" aria-labelledby="turn-h">
            <h2 id="turn-h">Turn outcome</h2>
            <p className="status" data-status={state.turnStatus}>{state.turnStatus}</p>
          </section>
        )}

        {state.conn === "version_mismatch" && (
          <section className="terminal-error" aria-labelledby="ver-h">
            <h2 id="ver-h">version_mismatch</h2>
            <p>Terminal. The connection closed; nothing further can be sent.</p>
          </section>
        )}

        {state.stderrs.length > 0 && (
          <section className="diagnostics" aria-labelledby="diag-h">
            <h2 id="diag-h">Engine diagnostics (stderr; not a lifecycle source)</h2>
            <pre>{state.stderrs.join("\n")}</pre>
          </section>
        )}

        <section className="transcript" aria-labelledby="wire-h">
          <h2 id="wire-h">Wire transcript (responses, verbatim)</h2>
          {state.responses.length === 0 && <p>none</p>}
          <pre>{state.responses.map((r) => JSON.stringify(r)).join("\n")}</pre>
        </section>
      </main>

      <footer>
        <button
          onClick={() => void sliceShutdown()}
          disabled={state.conn === "idle" || state.conn === "closed"}
        >
          Close transport (leaves any pending approval open for recovery)
        </button>
      </footer>
    </div>
  );
}

function ToolResultBody({ frame }: { frame: Frame }) {
  // The row is only rendered for a `tool.result` frame (App.tsx selects it by
  // event); this guard is what narrows the generated discriminated union so
  // the payload below is the typed `ToolResultPayload`.
  if (frame.event !== "tool.result") return null;
  const p = frame.payload;
  const status = String(p["status"] ?? "");
  if (status === "succeeded") {
    return (
      <div>
        <h3>Tool output (inert text)</h3>
        <pre className="output">{asText(p["output"])}</pre>
      </div>
    );
  }
  if (status === "indeterminate") {
    return (
      <div>
        <p>The outcome of this call is unknown. There is no retry control.</p>
        <pre className="output">{asText(p)}</pre>
      </div>
    );
  }
  // failed / denied / aborted: typed failure screen, nothing
  // success-shaped, abort_cause and error text verbatim.
  return (
    <div>
      {p["abort_cause"] !== undefined && (
        <p>abort_cause: {String(p["abort_cause"])}</p>
      )}
      <h3>Operation error</h3>
      <pre className="output">{asText(p["error"])}</pre>

    </div>
  );
}
