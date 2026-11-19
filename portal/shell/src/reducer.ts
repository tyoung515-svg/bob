// S10 §2 reducer (E6). Extracted verbatim from `App.tsx` in E10 so the JS
// reducer harness can drive the REAL reducer (not a copy of it) from the
// committed wire transcripts; there is no behaviour change here.
//
// Local, disposable projection domains. Everything derives from protocol
// frames/responses only; no local clock, timeout or countdown exists (C-2),
// and no mutating control stays enabled after the request closes.

import type { ApprovalPreview, Frame, ResponseLine } from "./protocol";

export type ConnState =
  | "idle"
  | "connecting"
  | "connected"
  | "version_mismatch"
  | "closed";

export interface Row {
  frame: Frame;
  violation: boolean;
}

export interface AppState {
  conn: ConnState;
  dataRoot: string | null;
  rows: Row[];
  /** correlation_id -> event_ids already seen (C-5 chain check). */
  seenByCall: Record<string, string[]>;
  /** frozen after a protocol_violation: the offending row keeps its
   * typed error; nothing later renders (C-5). */
  frozen: boolean;
  responses: ResponseLine[];
  pending: ApprovalPreview | null;
  requestClosed: boolean;
  notices: string[];
  stderrs: string[];
  exitCode: number | null;
  turnStatus: string | null;
  toolResultStatus: string | null;
}

export type Action =
  | { t: "spawned"; dataRoot: string }
  | { t: "frame"; frame: Frame }
  | { t: "response"; response: ResponseLine }
  | { t: "notice"; text: string }
  | { t: "stderr"; text: string }
  | { t: "exit"; code: number | null };

export const initial: AppState = {
  conn: "idle",
  dataRoot: null,
  rows: [],
  seenByCall: {},
  frozen: false,
  responses: [],
  pending: null,
  requestClosed: false,
  notices: [],
  stderrs: [],
  exitCode: null,
  turnStatus: null,
  toolResultStatus: null,
};

export function reducer(s: AppState, a: Action): AppState {
  switch (a.t) {
    case "spawned":
      return { ...s, conn: "connecting", dataRoot: a.dataRoot };
    case "frame": {
      if (s.frozen) return s;
      const f = a.frame;
      // C-5: a frame whose caused_by is not an already-seen event of the
      // same correlation_id is a typed protocol_violation; the row
      // freezes and nothing falls back to success.
      // EXEC-55: the FIRST frame of a correlation legitimately cites the
      // step.context record, which has no wire frame (S5 IN-1), so its
      // caused_by can never be in the seen set — the check applies only
      // once at least one frame of the correlation has been seen.
      const seenForCall = f.correlation_id !== null
        ? (s.seenByCall[f.correlation_id] ?? [])
        : [];
      const violation =
        f.correlation_id !== null &&
        f.caused_by !== null &&
        seenForCall.length > 0 &&
        !seenForCall.includes(f.caused_by);
      const seenByCall = violation
        ? s.seenByCall
        : {
            ...s.seenByCall,
            ...(f.correlation_id !== null
              ? {
                  [f.correlation_id]: [
                    ...(s.seenByCall[f.correlation_id] ?? []),
                    f.event_id,
                  ],
                }
              : {}),
          };
      const rows = [...s.rows, { frame: f, violation }];
      if (violation) return { ...s, rows, seenByCall, frozen: true };
      let pending = s.pending;
      let requestClosed = s.requestClosed;
      let toolResultStatus = s.toolResultStatus;
      let turnStatus = s.turnStatus;
      if (f.event === "approval.requested") {
        pending = f.payload;
      }
      if (f.event === "approval.resolved") {
        requestClosed = true;
      }
      if (f.event === "tool.result") {
        toolResultStatus = String(f.payload["status"] ?? "");
        if (pending !== null && f.correlation_id === pending.call_id) {
          requestClosed = true;
        }
      }
      if (f.event === "turn.completed") {
        turnStatus = String(f.payload["status"] ?? "");
      }
      return {
        ...s,
        rows,
        seenByCall,
        pending,
        requestClosed,
        toolResultStatus,
        turnStatus,
      };
    }
    case "response": {
      const responses = [...s.responses, a.response];
      let conn = s.conn;
      if (
        a.response.id === "r1" &&
        "error" in a.response &&
        a.response.error?.code === "version_mismatch"
      ) {
        conn = "version_mismatch";
      }
      // EXEC-56: the handshake completes with the HelloAck result — that
      // is when the connection is connected (this assignment was missing,
      // leaving the connection line on "connecting" forever).
      if (
        a.response.id === "r1" &&
        "result" in a.response &&
        s.conn === "connecting"
      ) {
        conn = "connected";
      }
      return { ...s, responses, conn };
    }
    case "notice":
      return { ...s, notices: [...s.notices, a.text] };
    case "stderr":
      return { ...s, stderrs: [...s.stderrs, a.text] };
    case "exit":
      return {
        ...s,
        exitCode: a.code,
        conn: s.conn === "version_mismatch" ? s.conn : "closed",
      };
  }
}
