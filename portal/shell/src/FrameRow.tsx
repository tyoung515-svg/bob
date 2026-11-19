import type { Frame } from "./protocol";

// One row per frame (S10 §2 6.2). Labels avoid "running": tool.started
// is the start witness — "authorized to attempt" — never a claim that
// execution is underway. Statuses render verbatim.

const EVENT_LABELS: Record<string, string> = {
  "turn.started": "turn started",
  "tool.call": "tool call recorded",
  "authority.decided": "authority decided",
  "approval.requested": "approval requested",
  "approval.resolved": "approval resolved",
  "tool.started": "start witness: authorized to attempt",
  "tool.result": "tool result",
  "turn.completed": "turn completed",
};

export function FrameRow({ row }: { row: { frame: Frame; violation: boolean } }) {
  const f = row.frame;
  const status =
    f.event === "tool.result" || f.event === "turn.completed"
      ? ` — ${String(f.payload["status"] ?? "")}`
      : "";
  return (
    <li className={row.violation ? "row violation" : "row"}>
      <span className="seq">seq {f.seq}</span>{" "}
      <span className="label">
        {EVENT_LABELS[f.event] ?? f.event}
        {status}
      </span>
      {row.violation ? (
        <span className="pv"> protocol_violation: caused_by {f.caused_by} is not a
        prior event of correlation_id {f.correlation_id}; this row is frozen</span>
      ) : (
        <span className="meta">
          {" "}
          ordinal {f.ordinal} · event_id {f.event_id} · ts {f.ts}
        </span>
      )}
    </li>
  );
}
