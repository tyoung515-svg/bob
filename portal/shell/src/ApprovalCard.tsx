import { useEffect, useRef, useState } from "react";
import { DENY_REASON_MAX_BYTES, type ApprovalPreview } from "./protocol";
import { asText } from "./text";

// The approval card (S10 §2 6.3 / C-1 / C-2): exactly the 9
// ApprovalPreview fields verbatim, allow_once / deny(reason?) controls
// that echo decision_id + args_digest, no expiry, no countdown, no risk
// grade, no boolean badge. Controls disable (state verbatim) once
// approval.resolved or a descendant tool.result closes the request.

interface Props {
  preview: ApprovalPreview;
  closed: boolean;
  resolution: Record<string, unknown> | null;
  tamperEcho: boolean;
  onTamperEchoChange: (v: boolean) => void;
  onAllow: () => void;
  onDeny: (reason: string) => void;
}

function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="field">
      <dt>{label}</dt>
      <dd>
        <code>{asText(value) || "—"}</code>
      </dd>
    </div>
  );
}

export function ApprovalCard({
  preview,
  closed,
  resolution,
  tamperEcho,
  onTamperEchoChange,
  onAllow,
  onDeny,
}: Props) {
  const [reason, setReason] = useState("");
  const [mode, setMode] = useState<"none" | "deny">("none");
  const reasonInput = useRef<HTMLTextAreaElement>(null);
  // Only an explicit expansion action moves focus. Protocol arrivals and
  // closure never steal it (C-10); the controls remain present when closed.
  useEffect(() => {
    if (mode === "deny" && !closed) reasonInput.current?.focus();
  }, [mode, closed]);

  return (
    <section className="approval" aria-labelledby="approval-h">
      <h2 id="approval-h">Approval requested</h2>

      <dl className="preview">
        <Field label="call_id" value={preview.call_id} />
        <Field label="decision_id" value={preview.decision_id} />
        <Field label="tool_name" value={preview.tool_name} />
        <Field label="action_summary" value={preview.action_summary} />
        <Field label="args_preview" value={preview.args_preview} />
        <Field label="args_truncated" value={String(preview.args_truncated)} />
        <Field label="args_digest" value={preview.args_digest} />
        <Field label="effect_class" value={preview.effect_class} />
        <Field label="targets" value={preview.targets} />
        <Field label="matched_rule" value={preview.matched_rule} />
        <Field label="requested_at" value={preview.requested_at} />
      </dl>

      {closed && (
        <p className="closed">
          This request is closed.{" "}
          {resolution
            ? `resolution: ${asText(resolution["resolution"]) || "—"} · actor: ${asText(resolution["actor"]) || "—"}`
            : ""}
        </p>
      )}
        <>
          <div className="controls">
            <button type="button" disabled={closed} onClick={onAllow}>allow_once</button>
            <button
              type="button"
              disabled={closed}
              aria-controls="deny-reason-panel"
              aria-expanded={mode === "deny"}
              onClick={() => setMode(mode === "deny" ? "none" : "deny")}
            >
              deny
            </button>
          </div>
          {mode === "deny" && (
            <div className="deny-reason" id="deny-reason-panel">
              <label htmlFor="deny-reason-input">
                Optional reason (free text, at most {DENY_REASON_MAX_BYTES} bytes)
              </label>
              <textarea
                id="deny-reason-input"
                ref={reasonInput}
                disabled={closed}
                rows={2}
                maxLength={DENY_REASON_MAX_BYTES}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button type="button" disabled={closed} className="confirm" onClick={() => onDeny(reason)}>
                Send deny{reason.trim().length > 0 ? " with reason" : ""}
              </button>
            </div>
          )}
          <fieldset className="verification" disabled={closed}>
            <legend>Verification</legend>
            <label>
              <input
                type="checkbox"
                checked={tamperEcho}
                onChange={(e) => onTamperEchoChange(e.target.checked)}
              />{" "}
              Corrupt the args_digest echo for the next response (expect
              the engine to answer approval_mismatch and keep this request
              pending)
            </label>
          </fieldset>
        </>
    </section>
  );
}
