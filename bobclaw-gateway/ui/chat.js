import { h } from "preact";
import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import htm from "htm";
import { apiFetch, getAccessToken, notifyUnauthorized, refreshAccessToken } from "./api.js";
import { Markdown } from "./markdown.js";
import { chatSocketUrl, isSocketOpen, sendJson } from "./ws.js";

const html = htm.bind(h);
const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 5000;

function createId(prefix) {
  if (globalThis.crypto && globalThis.crypto.randomUUID) {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }

  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function parseMetadata(metadata) {
  if (!metadata) {
    return {};
  }

  if (typeof metadata === "object") {
    return metadata;
  }

  try {
    return JSON.parse(metadata);
  } catch (_error) {
    return {};
  }
}

function normalizeHistoryMessage(item) {
  const metadata = parseMetadata(item.metadata);
  const role = item.role === "user" || item.role === "assistant" ? item.role : "assistant";

  return {
    id: item.id || createId("history"),
    serverId: item.id,
    role,
    content: item.content || "",
    streaming: false,
    meta: {
      backend: metadata.backend || metadata.resolved_backend,
      model: metadata.model,
      tokens_in: metadata.tokens_in,
      tokens_out: metadata.tokens_out,
      elapsed_ms: metadata.elapsed_ms
    }
  };
}

function previewText(text) {
  const compact = String(text || "").replace(/\s+/g, " ").trim();
  return compact.length > 120 ? `${compact.slice(0, 117)}...` : compact;
}

function statusLabel(status) {
  if (status === "connected") {
    return "Connected";
  }
  if (status === "reconnecting") {
    return "Reconnecting...";
  }
  if (status === "connecting") {
    return "Connecting...";
  }
  return "Offline";
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

function titleize(value) {
  return String(value || "approval").replace(/[_-]+/g, " ");
}

function MessageMeta({ message }) {
  const meta = message.meta || {};
  const parts = [];

  if (meta.backend || meta.model) {
    parts.push([meta.backend, meta.model].filter(Boolean).join(" / "));
  }

  if (Number.isFinite(meta.tokens_in) || Number.isFinite(meta.tokens_out)) {
    parts.push(`in ${meta.tokens_in || 0} / out ${meta.tokens_out || 0}`);
  }

  if (Number.isFinite(meta.elapsed_ms)) {
    parts.push(`${meta.elapsed_ms}ms`);
  }

  if (meta.stopped) {
    parts.push("stopped");
  }

  if (meta.interrupted) {
    parts.push("connection dropped");
  }

  if (!parts.length) {
    return null;
  }

  return html`<div class="message-meta">${parts.join(" · ")}</div>`;
}

function MessageBubble({ message, onApprovalDecision }) {
  if (message.role === "approval") {
    const approval = message.approval || {};
    const status = approval.status || "pending";
    const decided = status === "approve" || status === "reject";

    return html`
      <article class="message-row approval">
        <div class="message-bubble approval-request-card">
          <div class="message-author">Approval request</div>
          <div class="approval-request-title">${titleize(approval.action)}</div>
          <pre class="details-block">${formatDetails(approval.details)}</pre>
          ${approval.error ? html`<div class="inline-error" role="alert">${approval.error}</div>` : null}
          <div class="approval-actions">
            <button
              class="primary-button compact-button"
              type="button"
              disabled=${approval.deciding || decided}
              onClick=${() => onApprovalDecision(approval.id, "approve")}
            >
              ${approval.deciding === "approve" ? "Approving..." : "Approve"}
            </button>
            <button
              class="secondary-button compact-button"
              type="button"
              disabled=${approval.deciding || decided}
              onClick=${() => onApprovalDecision(approval.id, "reject")}
            >
              ${approval.deciding === "reject" ? "Rejecting..." : "Reject"}
            </button>
            ${decided
              ? html`<span class="message-meta">${status === "approve" ? "Approved" : "Rejected"}</span>`
              : null}
          </div>
        </div>
      </article>
    `;
  }

  if (message.role === "error") {
    return html`
      <article class="message-row error" role="alert">
        <div class="message-bubble">
          <div class="message-author">Error${message.code ? ` · ${message.code}` : ""}</div>
          <p>${message.content}</p>
        </div>
      </article>
    `;
  }

  const isAssistant = message.role === "assistant";
  const body =
    isAssistant
      ? html`
          <${Markdown} text=${message.content} />
          ${message.streaming && !message.content
            ? html`<div class="typing-indicator" aria-label="Assistant is responding"><span></span><span></span><span></span></div>`
            : null}
        `
      : html`<p>${message.content}</p>`;

  return html`
    <article class=${`message-row ${message.role}`}>
      <div class="message-bubble">
        <div class="message-author">${isAssistant ? "BobClaw" : "You"}</div>
        ${body}
        <${MessageMeta} message=${message} />
      </div>
    </article>
  `;
}

export function ChatPane({
  activeConversation,
  pinCommand,
  onPinAck = () => {},
  onWsStatusChange = () => {},
  onConversationTouched = () => {}
}) {
  const activeConversationId = activeConversation?.id || "";
  const [historyStatus, setHistoryStatus] = useState("idle");
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stopRequested, setStopRequested] = useState(false);
  const [wsStatus, setWsStatus] = useState("connecting");

  const socketRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  const authRefreshUsedRef = useRef(false);
  const currentAssistantIdRef = useRef(null);
  const assistantTextRef = useRef("");
  const streamingRef = useRef(false);
  const messagesEndRef = useRef(null);
  const onPinAckRef = useRef(onPinAck);
  const onConversationTouchedRef = useRef(onConversationTouched);

  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);

  useEffect(() => {
    onPinAckRef.current = onPinAck;
  }, [onPinAck]);

  useEffect(() => {
    onConversationTouchedRef.current = onConversationTouched;
  }, [onConversationTouched]);

  useEffect(() => {
    onWsStatusChange(wsStatus);
  }, [onWsStatusChange, wsStatus]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [messages, streaming]);

  const appendError = useCallback((message, code) => {
    setMessages((items) => [
      ...items,
      {
        id: createId("error"),
        role: "error",
        content: message || "Unexpected chat error.",
        code
      }
    ]);
  }, []);

  const updateAssistant = useCallback((updater) => {
    const assistantId = currentAssistantIdRef.current;
    if (!assistantId) {
      return;
    }

    setMessages((items) =>
      items.map((item) =>
        item.id === assistantId
          ? updater(item)
          : item
      )
    );
  }, []);

  const finishStreaming = useCallback(() => {
    currentAssistantIdRef.current = null;
    setStreaming(false);
    setStopRequested(false);
  }, []);

  useEffect(() => {
    if (!activeConversationId) {
      setMessages([]);
      setHistoryStatus("idle");
      finishStreaming();
      return;
    }

    let cancelled = false;

    if (streamingRef.current) {
      sendJson(socketRef.current, { type: "stop_generation" });
      finishStreaming();
    }

    currentAssistantIdRef.current = null;
    assistantTextRef.current = "";
    setInput("");
    setMessages([]);
    setHistoryStatus("loading");

    async function loadHistory() {
      try {
        const response = await apiFetch(`/conversations/${encodeURIComponent(activeConversationId)}/messages?limit=50`);
        if (cancelled || response.status === 401) {
          return;
        }

        if (!response.ok) {
          setHistoryStatus("error");
          appendError(`Unable to load messages (${response.status}).`, "history_load_failed");
          return;
        }

        const payload = await response.json();
        const items = (payload.items || []).map(normalizeHistoryMessage).reverse();
        setMessages(items);
        setHistoryStatus("ready");
      } catch (_error) {
        if (!cancelled) {
          setHistoryStatus("error");
          appendError("Unable to reach the messages endpoint.", "history_load_failed");
        }
      }
    }

    loadHistory();

    return () => {
      cancelled = true;
    };
  }, [activeConversationId, appendError, finishStreaming]);

  const handleServerEvent = useCallback(
    (event) => {
      if (!event || !event.type) {
        return;
      }

      if (event.type === "chunk") {
        assistantTextRef.current = `${assistantTextRef.current}${event.content || ""}`;
        updateAssistant((message) => ({
          ...message,
          content: `${message.content || ""}${event.content || ""}`,
          meta: {
            ...(message.meta || {}),
            backend: event.backend || message.meta?.backend,
            model: event.model || message.meta?.model
          }
        }));
        return;
      }

      if (event.type === "message_complete") {
        updateAssistant((message) => ({
          ...message,
          streaming: false,
          serverId: event.message_id || message.serverId,
          meta: {
            ...(message.meta || {}),
            tokens_in: event.tokens_in,
            tokens_out: event.tokens_out,
            elapsed_ms: event.elapsed_ms
          }
        }));
        onConversationTouchedRef.current(activeConversationId, {
          last_message_preview: previewText(assistantTextRef.current) || "Assistant replied"
        });
        finishStreaming();
        return;
      }

      if (event.type === "generation_stopped") {
        if (event.code === "superseded") {
          const assistantId = currentAssistantIdRef.current;
          if (assistantId) {
            setMessages((items) => items.filter((item) => item.id !== assistantId));
          }
        } else {
          updateAssistant((message) => ({
            ...message,
            streaming: false,
            meta: {
              ...(message.meta || {}),
              stopped: true
            }
          }));
          onConversationTouchedRef.current(activeConversationId, {
            last_message_preview: previewText(assistantTextRef.current) || "Stopped"
          });
        }
        finishStreaming();
        return;
      }

      if (event.type === "face_switched") {
        onPinAckRef.current(activeConversationId, {
          face_id: event.face_id,
          face_name: event.face_name
        });
        return;
      }

      if (event.type === "model_switched") {
        onPinAckRef.current(activeConversationId, {
          backend: event.backend,
          model: event.model
        });
        return;
      }

      if (event.type === "approval_request") {
        const approvalId = event.approval_id || event.id || createId("approval");
        setMessages((items) => {
          const existing = items.some(
            (item) => item.role === "approval" && item.approval?.id === approvalId
          );

          if (existing) {
            return items;
          }

          return [
            ...items,
            {
              id: `approval-${approvalId}`,
              role: "approval",
              approval: {
                id: approvalId,
                action: event.action || event.action_type || "approval",
                details: event.details || {},
                status: "pending"
              }
            }
          ];
        });
        return;
      }

      if (event.type === "error") {
        if (event.code === "no_active_generation") {
          return;
        }

        if (streamingRef.current) {
          updateAssistant((message) => ({
            ...message,
            streaming: false
          }));
          finishStreaming();
        }

        appendError(event.message, event.code);
      }
    },
    [activeConversationId, appendError, finishStreaming, updateAssistant]
  );

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

      const socket = new WebSocket(chatSocketUrl());
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
          appendError("Received an unreadable chat event.", "invalid_json");
          return;
        }

        if (firstFrame && payload.type === "error" && Date.now() - authSentAt < 1800) {
          socketRef.current = null;
          socket.close();
          handleAuthClose();
          return;
        }

        authRefreshUsedRef.current = false;
        handleServerEvent(payload);
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

        if (streamingRef.current) {
          updateAssistant((message) => ({
            ...message,
            streaming: false,
            meta: {
              ...(message.meta || {}),
              interrupted: true
            }
          }));
          finishStreaming();
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
  }, [appendError, finishStreaming, handleServerEvent, updateAssistant]);

  useEffect(() => {
    if (!pinCommand || !pinCommand.type) {
      return;
    }

    const { nonce: _nonce, ...payload } = pinCommand;
    const sent = sendJson(socketRef.current, payload);
    if (!sent) {
      appendError("Chat socket is not connected for pin changes.", "socket_not_connected");
    }
  }, [appendError, pinCommand]);

  function updateApprovalMessage(approvalId, patch) {
    setMessages((items) =>
      items.map((item) =>
        item.role === "approval" && item.approval?.id === approvalId
          ? {
              ...item,
              approval: {
                ...item.approval,
                ...patch
              }
            }
          : item
      )
    );
  }

  function handleInlineApprovalDecision(approvalId, decision) {
    if (!approvalId) {
      return;
    }

    updateApprovalMessage(approvalId, {
      deciding: decision,
      error: ""
    });

    const sent = sendJson(socketRef.current, {
      type: "approval_response",
      approval_id: approvalId,
      decision
    });

    if (!sent) {
      updateApprovalMessage(approvalId, {
        deciding: false,
        error: "Chat socket is not connected."
      });
      return;
    }

    updateApprovalMessage(approvalId, {
      deciding: false,
      status: decision
    });
  }

  const canSend = useMemo(
    () =>
      Boolean(input.trim()) &&
      Boolean(activeConversationId) &&
      historyStatus !== "loading" &&
      wsStatus === "connected" &&
      !streaming,
    [activeConversationId, historyStatus, input, streaming, wsStatus]
  );

  function handleSubmit(event) {
    event.preventDefault();

    const content = input.trim();
    if (!content || !activeConversationId || streaming) {
      return;
    }

    const assistantId = createId("assistant");
    currentAssistantIdRef.current = assistantId;
    assistantTextRef.current = "";
    setInput("");
    setStreaming(true);
    setStopRequested(false);

    setMessages((items) => [
      ...items,
      {
        id: createId("user"),
        role: "user",
        content
      },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        streaming: true,
        meta: {}
      }
    ]);

    onConversationTouched(activeConversationId, {
      last_message_preview: previewText(content)
    });

    const sent = sendJson(socketRef.current, {
      type: "message",
      conversation_id: activeConversationId,
      content
    });

    if (!sent) {
      updateAssistant((message) => ({
        ...message,
        streaming: false,
        meta: {
          ...(message.meta || {}),
          interrupted: true
        }
      }));
      finishStreaming();
      appendError("Chat socket is not connected.", "socket_not_connected");
    }
  }

  function handleStop() {
    if (!streaming || stopRequested) {
      return;
    }

    const sent = sendJson(socketRef.current, { type: "stop_generation" });
    if (sent) {
      setStopRequested(true);
      return;
    }

    updateAssistant((message) => ({
      ...message,
      streaming: false,
      meta: {
        ...(message.meta || {}),
        interrupted: true
      }
    }));
    finishStreaming();
  }

  function handleInputKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (canSend) {
        handleSubmit(event);
      }
    }
  }

  const emptyText = !activeConversationId
    ? "Select or create a conversation"
    : historyStatus === "loading"
      ? "Loading history..."
      : "Ready";

  return html`
    <section class="chat-pane" aria-label="Chat">
      <div class="chat-toolbar">
        <div>
          <p class="eyebrow">Chat</p>
          <h1>${activeConversation?.title || "BobClaw"}</h1>
        </div>
        <div class=${`ws-pill ${wsStatus}`}>
          <span class=${`status-dot ${wsStatus === "connected" ? "online" : "pending"}`}></span>
          ${statusLabel(wsStatus)}
        </div>
      </div>

      <div class="chat-messages" aria-live="polite">
        ${messages.length
          ? messages.map(
              (message) => html`
                <${MessageBubble}
                  key=${message.id}
                  message=${message}
                  onApprovalDecision=${handleInlineApprovalDecision}
                />
              `
            )
          : html`<div class="chat-empty">${emptyText}</div>`}
        <div ref=${messagesEndRef}></div>
      </div>

      <form class="chat-composer" onSubmit=${handleSubmit}>
        <textarea
          value=${input}
          rows="3"
          placeholder=${activeConversationId ? (wsStatus === "connected" ? "Message BobClaw" : "Reconnecting...") : "Create or select a conversation"}
          disabled=${!activeConversationId || historyStatus === "loading" || wsStatus !== "connected" || streaming}
          onInput=${(event) => setInput(event.currentTarget.value)}
          onKeyDown=${handleInputKeyDown}
        ></textarea>
        <div class="composer-actions">
          ${streaming
            ? html`
                <button class="secondary-button stop-button" type="button" disabled=${stopRequested} onClick=${handleStop}>
                  ${stopRequested ? "Stopping..." : "Stop"}
                </button>
              `
            : null}
          <button class="primary-button send-button" type="submit" disabled=${!canSend}>
            Send
          </button>
        </div>
      </form>
    </section>
  `;
}
