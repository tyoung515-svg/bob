import { h } from "preact";
import { useCallback, useEffect, useState } from "preact/hooks";
import htm from "htm";
import { apiFetch } from "./api.js";

const html = htm.bind(h);
const FACT_LIMIT = 50;

function normalizeFact(item = {}) {
  return {
    fact_id: item.fact_id || item.id || "",
    text: item.text || "",
    subject: item.subject || "",
    predicate: item.predicate || "",
    ts: item.ts || item.created_at || "",
    source_event_id: item.source_event_id || "",
    confidence: item.confidence
  };
}

function formatTime(value) {
  const timestamp = Date.parse(value || "");
  if (!timestamp) {
    return "";
  }

  return new Date(timestamp).toLocaleString();
}

function formatConfidence(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "";
  }

  if (numeric >= 0 && numeric <= 1) {
    return `${Math.round(numeric * 100)}%`;
  }

  return String(value);
}

function FactItem({ fact, confirming, forgetting, onConfirm, onCancel, onForget }) {
  const timestamp = formatTime(fact.ts);
  const confidence = formatConfidence(fact.confidence);

  return html`
    <article class="memory-item">
      <div class="memory-main">
        <p class="memory-text">${fact.text || "Untitled fact"}</p>
        <div class="panel-meta memory-meta">
          ${fact.subject || fact.predicate
            ? html`<span>${[fact.subject, fact.predicate].filter(Boolean).join(" / ")}</span>`
            : null}
          ${timestamp ? html`<time dateTime=${fact.ts}>${timestamp}</time>` : null}
          ${confidence ? html`<span>${confidence} confidence</span>` : null}
          ${fact.source_event_id ? html`<span>Source ${fact.source_event_id}</span>` : null}
        </div>
      </div>

      ${confirming
        ? html`
            <div class="confirm-row">
              <span>Forget this fact?</span>
              <button class="mini-button danger" type="button" disabled=${forgetting} onClick=${() => onForget(fact.fact_id)}>
                ${forgetting ? "Forgetting..." : "Confirm"}
              </button>
              <button class="mini-button" type="button" disabled=${forgetting} onClick=${onCancel}>Cancel</button>
            </div>
          `
        : html`
            <button class="mini-button danger forget-button" type="button" onClick=${() => onConfirm(fact.fact_id)}>
              Forget
            </button>
          `}
    </article>
  `;
}

export function MemoryPanel() {
  const [facts, setFacts] = useState([]);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState("");
  const [confirmingId, setConfirmingId] = useState("");
  const [forgettingId, setForgettingId] = useState("");

  const loadFacts = useCallback(async () => {
    setStatus("loading");
    setError("");

    try {
      const response = await apiFetch(`/memory/facts?limit=${FACT_LIMIT}&offset=0`);
      if (response.status === 401) {
        return;
      }

      if (response.status === 404 || response.status === 502) {
        setFacts([]);
        setStatus("unavailable");
        return;
      }

      if (!response.ok) {
        setFacts([]);
        setStatus("error");
        setError(`Unable to load memory facts (${response.status}).`);
        return;
      }

      const payload = await response.json();
      const items = Array.isArray(payload) ? payload : payload.items || [];
      setFacts(items.map(normalizeFact).filter((item) => item.fact_id));
      setStatus("ready");
    } catch (_error) {
      setFacts([]);
      setStatus("unavailable");
    }
  }, []);

  useEffect(() => {
    loadFacts();
  }, [loadFacts]);

  async function forgetFact(id) {
    if (!id) {
      return;
    }

    setError("");
    setForgettingId(id);

    try {
      const response = await apiFetch(`/memory/facts/${encodeURIComponent(id)}`, {
        method: "DELETE"
      });

      if (response.status === 401) {
        return;
      }

      if (response.status === 404 || response.status === 502) {
        setFacts([]);
        setStatus("unavailable");
        return;
      }

      if (!response.ok) {
        setError(`Unable to forget memory fact (${response.status}).`);
        return;
      }

      setFacts((items) => items.filter((item) => item.fact_id !== id));
      setConfirmingId("");
      setStatus("ready");
    } catch (_error) {
      setError("Unable to reach the memory endpoint.");
    } finally {
      setForgettingId("");
    }
  }

  return html`
    <section class="utility-panel memory-panel" aria-label="Memory browser">
      <div class="panel-head">
        <div>
          <p class="eyebrow">Memory</p>
          <h2>Facts</h2>
        </div>
        <button class="mini-button" type="button" disabled=${status === "loading"} onClick=${loadFacts}>
          Refresh
        </button>
      </div>

      ${error ? html`<div class="panel-error" role="alert">${error}</div>` : null}

      <div class="panel-list">
        ${status === "loading"
          ? html`<div class="panel-empty">Loading memory...</div>`
          : status === "unavailable"
            ? html`<div class="panel-empty">Memory API unavailable</div>`
            : facts.length
              ? facts.map(
                  (fact) => html`
                    <${FactItem}
                      key=${fact.fact_id}
                      fact=${fact}
                      confirming=${confirmingId === fact.fact_id}
                      forgetting=${forgettingId === fact.fact_id}
                      onConfirm=${setConfirmingId}
                      onCancel=${() => setConfirmingId("")}
                      onForget=${forgetFact}
                    />
                  `
                )
              : html`<div class="panel-empty">No learned facts yet</div>`}
      </div>
    </section>
  `;
}
