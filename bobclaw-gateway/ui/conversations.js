import { h } from "preact";
import { useCallback, useEffect, useState } from "preact/hooks";
import htm from "htm";
import { apiFetch } from "./api.js";

const html = htm.bind(h);
const ACTIVE_CONVERSATION_KEY = "bc_active_conversation";

function normalizeConversation(item) {
  return {
    ...item,
    id: item.id || item.conversation_id,
    title: item.title || "Untitled",
    last_message_preview: item.last_message_preview || "",
    updated_at: item.updated_at || item.created_at || ""
  };
}

function sortedConversations(items) {
  return [...items].sort((a, b) => {
    const aTime = Date.parse(a.updated_at || "") || 0;
    const bTime = Date.parse(b.updated_at || "") || 0;
    return bTime - aTime;
  });
}

function readStoredActiveId() {
  return localStorage.getItem(ACTIVE_CONVERSATION_KEY);
}

function storeActiveId(id) {
  if (id) {
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, id);
  } else {
    localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
  }
}

export function useConversations() {
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(readStoredActiveId());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const selectConversation = useCallback((id) => {
    setActiveId(id || null);
    storeActiveId(id || null);
  }, []);

  const loadConversations = useCallback(async () => {
    setLoading(true);
    setError("");

    try {
      const response = await apiFetch("/conversations?limit=20&offset=0");
      if (response.status === 401) {
        return;
      }

      if (!response.ok) {
        setError(`Unable to load conversations (${response.status}).`);
        return;
      }

      const payload = await response.json();
      const items = sortedConversations((payload.items || []).map(normalizeConversation));
      setConversations(items);

      const storedId = readStoredActiveId();
      if (storedId && items.some((item) => item.id === storedId)) {
        setActiveId(storedId);
      } else if (items.length) {
        selectConversation(items[0].id);
      } else {
        selectConversation(null);
      }
    } catch (_error) {
      setError("Unable to reach the conversations endpoint.");
    } finally {
      setLoading(false);
    }
  }, [selectConversation]);

  useEffect(() => {
    loadConversations();
  }, [loadConversations]);

  async function createConversation() {
    setError("");
    const response = await apiFetch("/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "New chat" })
    });

    if (response.status === 401) {
      return null;
    }

    if (!response.ok) {
      setError(`Unable to create conversation (${response.status}).`);
      return null;
    }

    const created = normalizeConversation(await response.json());
    setConversations((items) => sortedConversations([created, ...items.filter((item) => item.id !== created.id)]));
    selectConversation(created.id);
    return created;
  }

  async function renameConversation(id, title) {
    const nextTitle = title.trim();
    if (!id || !nextTitle) {
      return false;
    }

    const response = await apiFetch(`/conversations/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      body: JSON.stringify({ title: nextTitle })
    });

    if (response.status === 401) {
      return false;
    }

    if (!response.ok) {
      setError(`Unable to rename conversation (${response.status}).`);
      return false;
    }

    const renamed = normalizeConversation(await response.json());
    setConversations((items) =>
      sortedConversations(items.map((item) => (item.id === id ? { ...item, ...renamed } : item)))
    );
    return true;
  }

  async function archiveConversation(id) {
    if (!id) {
      return false;
    }

    const response = await apiFetch(`/conversations/${encodeURIComponent(id)}`, {
      method: "DELETE"
    });

    if (response.status === 401) {
      return false;
    }

    if (!response.ok) {
      setError(`Unable to archive conversation (${response.status}).`);
      return false;
    }

    setConversations((items) => {
      const remaining = items.filter((item) => item.id !== id);
      if (activeId === id) {
        selectConversation(remaining[0]?.id || null);
      }
      return remaining;
    });
    return true;
  }

  function touchConversation(id, patch) {
    if (!id) {
      return;
    }

    setConversations((items) =>
      sortedConversations(
        items.map((item) =>
          item.id === id
            ? {
                ...item,
                ...patch,
                updated_at: patch.updated_at || new Date().toISOString()
              }
            : item
        )
      )
    );
  }

  const activeConversation = conversations.find((item) => item.id === activeId) || null;

  return {
    conversations,
    activeId,
    activeConversation,
    loading,
    error,
    createConversation,
    renameConversation,
    archiveConversation,
    selectConversation,
    touchConversation,
    reloadConversations: loadConversations
  };
}

export function ConversationsSidebar({
  open,
  onClose,
  conversations,
  activeId,
  loading,
  error,
  onCreate,
  onSelect,
  onRename,
  onArchive
}) {
  const [editingId, setEditingId] = useState(null);
  const [draftTitle, setDraftTitle] = useState("");

  function startRename(conversation, event) {
    event.stopPropagation();
    setEditingId(conversation.id);
    setDraftTitle(conversation.title || "");
  }

  async function submitRename(event) {
    event.preventDefault();
    const renamed = await onRename(editingId, draftTitle);
    if (renamed) {
      setEditingId(null);
      setDraftTitle("");
    }
  }

  async function archiveItem(conversation, event) {
    event.stopPropagation();
    await onArchive(conversation.id);
  }

  return html`
    <aside class=${`sidebar ${open ? "open" : ""}`} data-slot="left-sidebar">
      <div class="sidebar-head">
        <h2>Conversations</h2>
        <button class="icon-button close-sidebar" type="button" aria-label="Close sidebar" onClick=${onClose}>
          <span></span>
          <span></span>
        </button>
      </div>

      <div class="sidebar-actions">
        <button class="primary-button new-chat-button" type="button" onClick=${onCreate}>New</button>
      </div>

      ${error ? html`<div class="sidebar-error" role="alert">${error}</div>` : null}

      <div class="conversation-list" aria-label="Conversation list">
        ${loading
          ? html`<div class="sidebar-empty">Loading...</div>`
          : conversations.length
            ? conversations.map((conversation) => {
                const active = conversation.id === activeId;
                const preview = conversation.last_message_preview || "No messages yet";

                return html`
                  <div
                    key=${conversation.id}
                    class=${`conversation-item ${active ? "active" : ""}`}
                    role="button"
                    tabIndex="0"
                    onClick=${() => {
                      onSelect(conversation.id);
                      onClose();
                    }}
                    onKeyDown=${(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelect(conversation.id);
                        onClose();
                      }
                    }}
                  >
                    ${editingId === conversation.id
                      ? html`
                          <form class="rename-form" onSubmit=${submitRename} onClick=${(event) => event.stopPropagation()}>
                            <input
                              value=${draftTitle}
                              autoFocus
                              onInput=${(event) => setDraftTitle(event.currentTarget.value)}
                            />
                            <button class="mini-button" type="submit">Save</button>
                            <button class="mini-button" type="button" onClick=${() => setEditingId(null)}>Cancel</button>
                          </form>
                        `
                      : html`
                          <div class="conversation-main">
                            <div class="conversation-title">${conversation.title || "Untitled"}</div>
                            <div class="conversation-preview">${preview}</div>
                          </div>
                          <div class="conversation-actions">
                            <button class="mini-button" type="button" onClick=${(event) => startRename(conversation, event)}>Rename</button>
                            <button class="mini-button danger" type="button" onClick=${(event) => archiveItem(conversation, event)}>Archive</button>
                          </div>
                        `}
                  </div>
                `;
              })
            : html`<div class="sidebar-empty">No conversations</div>`}
      </div>
    </aside>
  `;
}
