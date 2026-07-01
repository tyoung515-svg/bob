import { h } from "preact";
import { useEffect, useMemo, useState } from "preact/hooks";
import htm from "htm";
import { apiFetch } from "./api.js";

const html = htm.bind(h);
const PIN_STATE_KEY = "bc_conversation_pins";

function normalizeFace(face) {
  const id = face.id || face.face_id;
  return {
    ...face,
    id,
    name: face.name || face.face_name || id
  };
}

function normalizeModel(model) {
  const backend = model.backend || model.id || model.name;
  return {
    ...model,
    backend,
    label: model.label || model.name || backend,
    available: model.available !== false
  };
}

function loadPinState() {
  try {
    return JSON.parse(localStorage.getItem(PIN_STATE_KEY) || "{}");
  } catch (_error) {
    return {};
  }
}

function storePinState(state) {
  localStorage.setItem(PIN_STATE_KEY, JSON.stringify(state));
}

function ackText(ack) {
  if (!ack) {
    return "";
  }

  const parts = [];
  if (ack.face_name || ack.face_id) {
    parts.push(`Face ${ack.face_name || ack.face_id}`);
  }

  if (ack.backend || ack.model) {
    parts.push([ack.backend, ack.model].filter(Boolean).join(" / "));
  }

  return parts.length ? `Ack: ${parts.join(" · ")}` : "";
}

export function PinsPanel({ activeConversation, disabled, onPinCommand, pinAck }) {
  const [faces, setFaces] = useState([]);
  const [facesError, setFacesError] = useState("");
  const [models, setModels] = useState([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [pinState, setPinState] = useState(loadPinState);

  useEffect(() => {
    let cancelled = false;

    async function loadFaces() {
      try {
        const response = await apiFetch("/faces");
        if (cancelled || response.status === 401) {
          return;
        }

        if (!response.ok) {
          setFacesError(`Faces unavailable (${response.status}).`);
          return;
        }

        const payload = await response.json();
        const items = Array.isArray(payload) ? payload : payload.items || payload.faces || [];
        setFaces(items.map(normalizeFace).filter((face) => face.id));
      } catch (_error) {
        if (!cancelled) {
          setFacesError("Faces unavailable.");
        }
      }
    }

    async function loadModels() {
      try {
        const response = await apiFetch("/models/available");
        if (cancelled || response.status === 401 || !response.ok) {
          return;
        }

        const payload = await response.json();
        const items = Array.isArray(payload) ? payload : payload.items || payload.models || [];
        setModels(items.map(normalizeModel).filter((model) => model.backend));
        setModelsLoaded(true);
      } catch (_error) {
        if (!cancelled) {
          setModelsLoaded(false);
        }
      }
    }

    loadFaces();
    loadModels();

    return () => {
      cancelled = true;
    };
  }, []);

  const conversationId = activeConversation?.id || "";
  const currentPins = useMemo(
    () => pinState[conversationId] || { face: activeConversation?.face_id || "auto", backend: "auto" },
    [activeConversation, conversationId, pinState]
  );

  function updatePin(kind, value) {
    if (!conversationId) {
      return;
    }

    const nextPins = {
      ...currentPins,
      [kind]: value
    };

    const nextState = {
      ...pinState,
      [conversationId]: nextPins
    };
    setPinState(nextState);
    storePinState(nextState);

    // "Auto" sends an EMPTY value, which the gateway treats as "clear this
    // conversation's pin" (back to unpinned face routing). Sending nothing
    // would leave a previously-set pin stuck on the backend.
    if (kind === "face") {
      onPinCommand({
        type: "switch_face",
        face_id: value === "auto" ? "" : value,
        conversation_id: conversationId
      });
    } else {
      onPinCommand({
        type: "switch_model",
        backend: value === "auto" ? "" : value,
        conversation_id: conversationId
      });
    }
  }

  return html`
    <section class="pins-panel" aria-label="Conversation pins">
      <div class="pin-row">
        <label>
          <span>Face</span>
          <select
            value=${currentPins.face || "auto"}
            disabled=${disabled || !conversationId}
            onChange=${(event) => updatePin("face", event.currentTarget.value)}
          >
            <option value="auto">Auto</option>
            ${faces.map((face) => html`<option key=${face.id} value=${face.id}>${face.name}</option>`)}
          </select>
        </label>
      </div>

      ${modelsLoaded
        ? html`
            <div class="pin-row">
              <label>
                <span>Backend</span>
                <select
                  value=${currentPins.backend || "auto"}
                  disabled=${disabled || !conversationId}
                  onChange=${(event) => updatePin("backend", event.currentTarget.value)}
                >
                  <option value="auto">Auto</option>
                  ${models.map(
                    (model) => html`
                      <option key=${model.backend} value=${model.backend} disabled=${!model.available}>
                        ${model.label}
                      </option>
                    `
                  )}
                </select>
              </label>
            </div>
          `
        : null}

      ${facesError ? html`<div class="pin-note error">${facesError}</div>` : null}
      ${ackText(pinAck) ? html`<div class="pin-note">${ackText(pinAck)}</div>` : html`<div class="pin-note">Auto uses face routing.</div>`}
    </section>
  `;
}
