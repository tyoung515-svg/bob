import { h } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import htm from "htm";
import { apiFetch, getAccessToken, notifyUnauthorized, refreshAccessToken } from "./api.js";
import { isSocketOpen, sendJson } from "./ws.js";

const html = htm.bind(h);
const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 5000;

function approvalsSocketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws/approvals`;
}

function normalizeApproval(item = {}) {
  const id = item.id || item.approval_id || "";

  return {
    id,
    conversation_id: item.conversation_id || "",
    user_id: item.user_id || "",
    action_type: item.action_type || item.action || "approval",
    details: item.details || {},
    status: item.status || "pending",
    decided_at: item.decided_at || "",
    created_at: item.created_at || item.ts || ""
  };
}

function formatDetails(details) {
  if (details === null || details === undefined || details === "") {
    return "No details";
  }

  if (typeof details === "string") {
    return details;
  }

  try {
    return JSON.stringify(details, null, 2);
  } catch (_error) {
    return String(details);
  }
}

function formatTime(value) {
  const timestamp = Date.parse(value || "");
  if (!timestamp) {
    return "";
  }

  return new Date(timestamp).toLocaleString();
}

function titleize(value) {
  return String(value || "approval").replace(/[_-]+/g, " ");
}

function socketLabel(status) {
  if (status === "connected") {
    return "Live";
  }
  if (status === "reconnecting") {
    return "Reconnecting";
  }
  if (status === "connecting") {
    return "Connecting";
  }
  return "Offline";
}

function mergeApproval(items, approval) {
  if (!approval.id) {
    return items;
  }

  return [
    approval,
    ...items.filter((item) => item.id !== approval.id)
  ].slice(0, 50);
}

function ApprovalItem({ approval, busyDecision, onDecide }) {
  const createdAt = formatTime(approval.created_at);

  return html`
    <article class="approval-item">
      <div class="approval-item-head">
        <div>
          <h3>${titleize(approval.action_type)}</h3>
          ${approval.conversation_id
            ? html`<p class="panel-meta">Conversation ${approval.conversation_id}</p>`
            : null}
        </div>
        ${createdAt ? html`<time class="panel-time" dateTime=${approval.created_at}>${createdAt}</time>` : null}
      </div>

      <pre class="details-block">${formatDetails(approval.details)}</pre>

      <div class="approval-actions">
        <button
          class="primary-button compact-button"
          type="button"
          disabled=${Boolean(busyDecision)}
          onClick=${() => onDecide(approval.id, "approve")}
        >
          ${busyDecision === "approve" ? "Approving..." : "Approve"}
        </button>
        <button
          class="secondary-button compact-button"
          type="button"
          disabled=${Boolean(busyDecision)}
          onClick=${() => onDecide(approval.id, "reject")}
        >
          ${busyDecision === "reject" ? "Rejecting..." : "Reject"}
        </button>
      </div>
    </article>
  `;
}

export function ApprovalsPanel({ onCountChange = () => {} }) {
  const [approvals, setApprovals] = useState([]);
  const [loadStatus, setLoadStatus] = useState("loading");
  const [error, setError] = useState("");
  const [wsStatus, setWsStatus] = useState("connecting");
  const [deciding, setDeciding] = useState({});

  const socketRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  const authRefreshUsedRef = useRef(false);

  useEffect(() => {
    onCountChange(approvals.length);
  }, [approvals.length, onCountChange]);

  const loadApprovals = useCallback(async () => {
    setLoadStatus("loading");
    setError("");

    try {
      const response = await apiFetch("/approvals?status=pending&limit=50");
      if (response.status === 401) {
        return;
      }

      if (!response.ok) {
        setLoadStatus("error");
        setError(`Unable to load approvals (${response.status}).`);
        return;
      }

      const payload = await response.json();
      const items = Array.isArray(payload) ? payload : payload.items || [];
      setApprovals(items.map(normalizeApproval).filter((item) => item.id));
      setLoadStatus("ready");
    } catch (_error) {
      setLoadStatus("error");
      setError("Unable to reach the approvals endpoint.");
    }
  }, []);

  useEffect(() => {
    loadApprovals();
  }, [loadApprovals]);

  const appendLiveApproval = useCallback((payload) => {
    const approval = normalizeApproval(payload);
    setApprovals((items) => mergeApproval(items, approval));
    setLoadStatus("ready");
  }, []);

  useEffect(() => {
    let cancelled = false;

    function clearReconnectTimer() {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    }

    function scheduleReconnect(delayOverride) {
      if (cancelled) {
        return;
      }

      clearReconnectTimer();
      const attempt = reconnectAttemptRef.current;
      const delay =
        Number.isFinite(delayOverride)
          ? delayOverride
          : Math.min(INITIAL_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);

      reconnectAttemptRef.current = attempt + 1;
      setWsStatus("reconnecting");
      reconnectTimerRef.current = setTimeout(connect, delay);
    }

    async function handleAuthClose() {
      if (authRefreshUsedRef.current) {
        notifyUnauthorized();
        return;
      }

      authRefreshUsedRef.current = true;

      let refreshed = false;
      try {
        refreshed = await refreshAccessToken();
      } catch (_error) {
        refreshed = false;
      }

      if (!refreshed) {
        notifyUnauthorized();
        return;
      }

      reconnectAttemptRef.current = 0;
      scheduleReconnect(0);
    }

    function connect() {
      if (cancelled) {
        return;
      }

      const token = getAccessToken();
      if (!token) {
        notifyUnauthorized();
        return;
      }

      setWsStatus(reconnectAttemptRef.current > 0 ? "reconnecting" : "connecting");

      const socket = new WebSocket(approvalsSocketUrl());
      let receivedFrame = false;
      let opened = false;
      let authSentAt = 0;
      socketRef.current = socket;

      socket.addEventListener("open", () => {
        opened = true;
        authSentAt = Date.now();
        sendJson(socket, {
          type: "auth",
          token
        });
        setWsStatus("connected");
        reconnectAttemptRef.current = 0;
      });

      socket.addEventListener("message", (event) => {
        const firstFrame = !receivedFrame;
        receivedFrame = true;

        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch (_error) {
          setError("Received an unreadable approvals event.");
          return;
        }

        if (firstFrame && payload.type === "error" && Date.now() - authSentAt < 1800) {
          socketRef.current = null;
          socket.close();
          handleAuthClose();
          return;
        }

        authRefreshUsedRef.current = false;
        if (payload.type === "new_approval") {
          appendLiveApproval(payload);
        }
      });

      socket.addEventListener("close", () => {
        if (cancelled || socketRef.current !== socket) {
          return;
        }

        socketRef.current = null;

        const authLikeClose = opened && !receivedFrame && Date.now() - authSentAt < 1800;
        if (authLikeClose) {
          handleAuthClose();
          return;
        }

        scheduleReconnect();
      });

      socket.addEventListener("error", () => {
        if (!opened || !receivedFrame) {
          return;
        }

        setWsStatus("reconnecting");
      });
    }

    connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      if (isSocketOpen(socketRef.current)) {
        socketRef.current.close(1000, "unmount");
      }
      socketRef.current = null;
    };
  }, [appendLiveApproval]);

  async function decideApproval(id, decision) {
    if (!id) {
      return;
    }

    setError("");
    setDeciding((items) => ({
      ...items,
      [id]: decision
    }));

    try {
      const response = await apiFetch(`/approvals/${encodeURIComponent(id)}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision })
      });

      if (response.status === 401) {
        return;
      }

      if (!response.ok) {
        setError(`Unable to ${decision} approval (${response.status}).`);
        return;
      }

      setApprovals((items) => items.filter((item) => item.id !== id));
    } catch (_error) {
      setError("Unable to reach the approval decision endpoint.");
    } finally {
      setDeciding((items) => {
        const next = { ...items };
        delete next[id];
        return next;
      });
    }
  }

  return html`
    <section class="utility-panel approvals-panel" aria-label="Approvals inbox">
      <div class="panel-head">
        <div>
          <p class="eyebrow">Approvals</p>
          <h2>Pending ${approvals.length}</h2>
        </div>
        <div class=${`ws-pill compact ${wsStatus}`}>
          <span class=${`status-dot ${wsStatus === "connected" ? "online" : "pending"}`}></span>
          ${socketLabel(wsStatus)}
        </div>
      </div>

      ${error ? html`<div class="panel-error" role="alert">${error}</div>` : null}

      <div class="panel-list">
        ${loadStatus === "loading"
          ? html`<div class="panel-empty">Loading approvals...</div>`
          : approvals.length
            ? approvals.map(
                (approval) => html`
                  <${ApprovalItem}
                    key=${approval.id}
                    approval=${approval}
                    busyDecision=${deciding[approval.id]}
                    onDecide=${decideApproval}
                  />
                `
              )
            : html`<div class="panel-empty">No pending approvals</div>`}
      </div>
    </section>
  `;
}
