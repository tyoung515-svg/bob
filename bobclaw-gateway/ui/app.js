import { h, render } from "preact";
import { useEffect, useMemo, useState } from "preact/hooks";
import htm from "htm";
import {
  ACCESS_KEY,
  REFRESH_KEY,
  clearSessionTokens,
  getAccessToken,
  getRefreshToken,
  hasSessionTokens,
  setSessionTokens,
  setUnauthorizedHandler
} from "./api.js";
import { ApprovalsPanel } from "./approvals.js";
import { ChatPane } from "./chat.js";
import { ConversationsSidebar, useConversations } from "./conversations.js";
import { MemoryPanel } from "./memory.js";
import { PinsPanel } from "./pins.js";
import { StatusStrip } from "./status.js";

const html = htm.bind(h);

function LoginScreen({ onAuthed }) {
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);

    const code = totp.trim();

    try {
      const response = await fetch("/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          password,
          totp_code: code || null
        })
      });

      if (response.status === 401) {
        setError("Invalid credentials or TOTP");
        return;
      }

      if (!response.ok) {
        setError("Login failed. Check the gateway and try again.");
        return;
      }

      const payload = await response.json();
      if (!payload.access_token || !payload.refresh_token) {
        setError("Login returned an incomplete token response.");
        return;
      }

      setSessionTokens(payload);
      setPassword("");
      setTotp("");
      onAuthed();
    } catch (_error) {
      setError("Unable to reach the gateway.");
    } finally {
      setBusy(false);
    }
  }

  return html`
    <main class="login-screen">
      <section class="login-panel" aria-labelledby="login-title">
        <div class="brand-lockup">
          <div class="brand-mark" aria-hidden="true">BC</div>
          <div>
            <p class="eyebrow">BobClaw</p>
            <h1 id="login-title">Sign in</h1>
          </div>
        </div>

        <form class="login-form" onSubmit=${handleSubmit}>
          <label class="field">
            <span>Password</span>
            <input
              type="password"
              value=${password}
              autoComplete="current-password"
              required
              disabled=${busy}
              onInput=${(event) => setPassword(event.currentTarget.value)}
            />
          </label>

          <label class="field">
            <span>TOTP (if enabled)</span>
            <input
              type="text"
              value=${totp}
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]*"
              disabled=${busy}
              onInput=${(event) => setTotp(event.currentTarget.value)}
            />
          </label>

          ${error &&
          html`<div class="form-error" role="alert">${error}</div>`}

          <button class="primary-button" type="submit" disabled=${busy}>
            ${busy ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  `;
}

function AuthedShell({ onLogout }) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [pinCommand, setPinCommand] = useState(null);
  const [pinAcks, setPinAcks] = useState({});
  const [chatSocketStatus, setChatSocketStatus] = useState("connecting");
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const conversations = useConversations();
  const shellClass = useMemo(
    () => `app-shell ${sidebarOpen ? "sidebar-visible" : ""}`,
    [sidebarOpen]
  );

  function queuePinCommand(command) {
    setPinCommand({
      ...command,
      nonce: `${Date.now()}-${Math.random().toString(16).slice(2)}`
    });
  }

  function handlePinAck(conversationId, ack) {
    if (!conversationId) {
      return;
    }

    setPinAcks((items) => ({
      ...items,
      [conversationId]: {
        ...(items[conversationId] || {}),
        ...ack
      }
    }));
  }

  async function handleLogout() {
    setLogoutBusy(true);

    const refreshToken = getRefreshToken();
    const accessToken = getAccessToken();

    try {
      if (refreshToken) {
        const headers = {
          "Content-Type": "application/json"
        };

        if (accessToken) {
          headers.Authorization = `Bearer ${accessToken}`;
        }

        await fetch("/auth/logout", {
          method: "POST",
          headers,
          body: JSON.stringify({ refresh_token: refreshToken })
        });
      }
    } catch (_error) {
      // Local logout still wins if the network or token state has already expired.
    } finally {
      clearSessionTokens();
      setLogoutBusy(false);
      onLogout();
    }
  }

  return html`
    <div class=${shellClass}>
      <header class="app-header">
        <div class="header-left">
          <button
            class="icon-button menu-button"
            type="button"
            aria-label="Toggle sidebar"
            aria-expanded=${sidebarOpen}
            onClick=${() => setSidebarOpen((value) => !value)}
          >
            <span></span>
            <span></span>
            <span></span>
          </button>
          <div class="header-title">
            <span class="brand-dot" aria-hidden="true"></span>
            <strong>BobClaw</strong>
          </div>
        </div>

        <div class="header-meta">
          <${StatusStrip} />
          <div class=${`approval-badge ${pendingApprovals ? "has-items" : ""}`} aria-label=${`${pendingApprovals} pending approvals`}>
            <span>Approvals</span>
            <strong>${pendingApprovals}</strong>
          </div>
        </div>

        <button class="secondary-button" type="button" disabled=${logoutBusy} onClick=${handleLogout}>
          ${logoutBusy ? "Logging out..." : "Logout"}
        </button>
      </header>

      <${ConversationsSidebar}
        open=${sidebarOpen}
        onClose=${() => setSidebarOpen(false)}
        conversations=${conversations.conversations}
        activeId=${conversations.activeId}
        loading=${conversations.loading}
        error=${conversations.error}
        onCreate=${conversations.createConversation}
        onSelect=${conversations.selectConversation}
        onRename=${conversations.renameConversation}
        onArchive=${conversations.archiveConversation}
      />
      <button
        class="drawer-backdrop"
        type="button"
        aria-label="Close sidebar"
        onClick=${() => setSidebarOpen(false)}
      ></button>

      <main class="content-shell" data-slot="main-content">
        <section class="workspace-main">
          <${PinsPanel}
            activeConversation=${conversations.activeConversation}
            disabled=${chatSocketStatus !== "connected"}
            onPinCommand=${queuePinCommand}
            pinAck=${conversations.activeConversation ? pinAcks[conversations.activeConversation.id] : null}
          />
          <${ChatPane}
            activeConversation=${conversations.activeConversation}
            pinCommand=${pinCommand}
            onPinAck=${handlePinAck}
            onWsStatusChange=${setChatSocketStatus}
            onConversationTouched=${conversations.touchConversation}
          />
        </section>
        <aside class="utility-panels" aria-label="Workspace panels">
          <${ApprovalsPanel} onCountChange=${setPendingApprovals} />
          <${MemoryPanel} />
        </aside>
      </main>
    </div>
  `;
}

function App() {
  const [authed, setAuthed] = useState(hasSessionTokens());

  useEffect(() => {
    setUnauthorizedHandler(() => setAuthed(false));

    function handleStorage(event) {
      if (event.key === ACCESS_KEY || event.key === REFRESH_KEY) {
        setAuthed(hasSessionTokens());
      }
    }

    window.addEventListener("storage", handleStorage);

    return () => {
      setUnauthorizedHandler(null);
      window.removeEventListener("storage", handleStorage);
    };
  }, []);

  if (!authed) {
    return html`<${LoginScreen} onAuthed=${() => setAuthed(true)} />`;
  }

  return html`<${AuthedShell} onLogout=${() => setAuthed(false)} />`;
}

render(html`<${App} />`, document.getElementById("app"));
