import { h } from "preact";
import { useEffect, useState } from "preact/hooks";
import htm from "htm";

const html = htm.bind(h);

function normalizeService(value) {
  if (value === true) {
    return "online";
  }

  if (value === false) {
    return "error";
  }

  const raw = typeof value === "object" && value !== null ? value.status || value.state || value.ok : value;
  if (raw === true) {
    return "online";
  }
  if (raw === false) {
    return "error";
  }

  const status = String(raw || "").toLowerCase();
  if (["ok", "healthy", "up", "ready", "running", "online"].includes(status)) {
    return "online";
  }
  if (["error", "down", "failed", "unhealthy", "offline"].includes(status)) {
    return "error";
  }
  return "pending";
}

function serviceLabel(key) {
  if (key === "claude_pipeline") {
    return "Pipeline";
  }
  return key.charAt(0).toUpperCase() + key.slice(1);
}

export function StatusStrip() {
  const [services, setServices] = useState({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function loadHealth() {
      try {
        const response = await fetch("/health");
        if (!response.ok) {
          throw new Error("health");
        }

        const payload = await response.json();
        if (!cancelled) {
          setServices(payload.services || {});
          setLoaded(true);
        }
      } catch (_error) {
        if (!cancelled) {
          setServices({});
          setLoaded(false);
        }
      }
    }

    loadHealth();
    const interval = setInterval(loadHealth, 10000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const keys = ["core", "claude_pipeline", "canopy"];

  return html`
    <div class="status-strip" data-slot="status-strip" aria-label="System status">
      ${keys.map((key) => {
        const state = loaded ? normalizeService(services[key]) : "pending";
        return html`
          <span class="status-chip" key=${key}>
            <span class=${`status-dot ${state}`}></span>
            ${serviceLabel(key)}
          </span>
        `;
      })}
      <span class="status-chip muted">Memory</span>
    </div>
  `;
}
