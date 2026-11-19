import { useEffect, useRef, useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';
import { toPortalError } from './portal-protocol';

export type EmbedderModel = '4b' | '0.6b';
export type EmbedderStatus = {
  selected_model: EmbedderModel; ready: boolean; file_present: boolean;
  sha_ok: boolean | null; service_active: boolean; embedding_dimension: number;
  recommended_model: EmbedderModel; ram_gb: number | null;
  // Newer runtimes: whether the selected weights are loaded, which model is served, and what is installed.
  model_loaded?: boolean | null; alias?: string | null; configured?: boolean | null; installed_models?: EmbedderModel[] | null;
  setup: { model: EmbedderModel; phase: string; detail: string } | null;
};
// Model size (parameters, download) and vector size (output dimensions) are different things.
export const EMBEDDER_MODELS: Record<EmbedderModel, { label: string; short: string; dimension: number; download: string }> = {
  '0.6b': { label: '0.6B — Lightweight', short: '0.6B', dimension: 1024, download: '~0.6 GB' },
  '4b': { label: '4B — Quality-focused', short: '4B', dimension: 2560, download: '~2.4 GB' },
};
const pending = (status: EmbedderStatus | null) => !!status?.setup && ['queued', 'downloading', 'verified', 'starting'].includes(status.setup.phase);
// Missing legacy configured metadata is not proof of a fresh runtime.
const configuredModel = (status: EmbedderStatus | null) => status && (status.file_present || status.configured !== false) ? status.selected_model : null;

/** One sentence of what is actually known, from the runtime's own receipt. */
export function embedderTruth(status: EmbedderStatus): { ready: boolean; text: string } {
  const model = EMBEDDER_MODELS[status.selected_model];
  if (status.ready) {
    return { ready: true, text: `Embedder ready · ${model.short} · ${status.alias ?? `${model.short} model`} · ${status.embedding_dimension}-dimension vectors.` };
  }
  if (status.file_present && status.sha_ok && !status.model_loaded) {
    return { ready: false, text: `${model.short} model files are downloaded and verified, but not loaded yet. Memory stays keyword-only until the embedder reports ready.` };
  }
  if (status.file_present && status.sha_ok !== true) {
    return { ready: false, text: `${model.short} model files are present, but their SHA256 is ${status.sha_ok === false ? 'not verified' : 'unknown'}. Embedder not ready · keyword-only recall.` };
  }
  return { ready: false, text: `${model.short} embedder is not ready. Semantic search is unavailable; recall remains keyword-only until the embedder is installed and healthy.` };
}

export function EmbedderSetup({ invoke, onContinue }: {
  invoke: NativeInvoke; onContinue?: (skipped: boolean) => void;
}) {
  const [status, setStatus] = useState<EmbedderStatus | null>(null);
  const [model, setModel] = useState<EmbedderModel | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [skipped, setSkipped] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const alive = useRef(false), touched = useRef(false), running = useRef(false);
  useEffect(() => {
    alive.current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await invoke<EmbedderStatus>('runtime_embedder_status');
        if (!alive.current) return;
        setStatus(next); setError('');
        // Never second-guess what is installed: only a fresh runtime follows the recommendation.
        if (!touched.current) setModel(pending(next) && next.setup ? next.setup.model
          : configuredModel(next) ?? next.recommended_model);
        if (pending(next)) timer = setTimeout(() => void poll(), 1500);
      } catch (reason) { if (alive.current) { setStatus(null); setError(toPortalError(reason).detail); } }
    };
    void poll();
    return () => { alive.current = false; if (timer) clearTimeout(timer); };
  }, [invoke, refresh]);
  async function install() {
    if (running.current || pending(status) || !status || !model) return;
    running.current = true; touched.current = true; setBusy(true); setError(''); setSkipped(false);
    try {
      let next = await invoke<EmbedderStatus>('runtime_embedder_install', { model });
      if (alive.current) setStatus(next);
      // The root job survives page closure. Remounting resumes status polling.
      while (alive.current && pending(next)) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (!alive.current) return;
        next = await invoke<EmbedderStatus>('runtime_embedder_status');
        if (alive.current) setStatus(next);
      }
    } catch (reason) { if (alive.current) setError(toPortalError(reason).detail); }
    finally {
      running.current = false;
      if (alive.current) { setBusy(false); window.dispatchEvent(new Event('bob:embedder-status')); }
    }
  }
  const working = busy || pending(status);
  const selected = configuredModel(status);
  const installed = status?.file_present ? status.selected_model : null;
  const switching = !!selected && !!model && model !== selected;
  const fresh = !!status && !selected;
  const truth = status ? embedderTruth(status) : null;
  const option = (key: EmbedderModel, quality: string) => <>
    <label><input type="radio" name="embedder-model" value={key} checked={model === key}
      onChange={() => { touched.current = true; setModel(key); }} />
      {EMBEDDER_MODELS[key].label}
      {fresh && status?.recommended_model === key && key === '0.6b' ? ' · Recommended starting option for most PCs' : ''}
      {installed === key ? ' · Installed' : ''}</label>
    <p>{quality} {EMBEDDER_MODELS[key].dimension}-dimension vectors · {EMBEDDER_MODELS[key].download} download.</p>
  </>;
  return <section className="set-card embedder-setup" aria-label="Embedder setup">
    <h2>Embedder</h2>
    <p>An embedder lets Bob find memories by meaning and runs inside Bob on this PC.</p>
    {status?.ram_gb != null && <p>Detected runtime RAM: {status.ram_gb} GB.</p>}
    <fieldset disabled={working || !status}><legend>Choose your memory model</legend>
      {option('0.6b', 'Uses fewer resources; a good starting point on most PCs.')}
      {option('4b', 'Uses more resources; a higher retrieval-quality option.')}
    </fieldset>
    <p>Bob downloads the selected weights from Hugging Face and checks their SHA256. Model size and vector size are different: BoB&apos;s own recall quality for each choice has not been measured yet.</p>
    <p>Changing models after memories are indexed requires re-embedding; automatic re-embedding is not available.</p>
    {selected && !status?.file_present && <p className="caveat" role="note">{EMBEDDER_MODELS[selected].short} is selected, but its weights are missing. Install this model to repair it, or explicitly choose another model.</p>}
    {switching && model && selected && <p className="caveat" role="note">Switching from {EMBEDDER_MODELS[selected].short} to {EMBEDDER_MODELS[model].short} needs re-embedding of your existing memories. Bob refuses the switch while memory vectors exist.</p>}
    {truth && (truth.ready ? <p role="status">{truth.text}</p> : <p className="caveat" role="status">{truth.text}</p>)}
    {skipped && !status?.ready && <p role="status">Skipped for now. Memory stays keyword-only (no semantic search). Set up the embedder any time in Settings → Models.</p>}
    {status?.setup && <p role={status.setup.phase === 'failed' ? 'alert' : 'status'} aria-live="polite">{status.setup.detail}</p>}
    {working && <progress aria-label="Embedder installation progress" />}
    {error && <p role="alert">{error}</p>}
    <div className="row-actions">
      <button className="primary" disabled={working || !status || !model} onClick={() => void install()}>
        {switching && model ? `Switch to ${EMBEDDER_MODELS[model].short}` : 'Install embedder'}</button>
      <button disabled={busy} onClick={() => setRefresh(value => value + 1)}>Refresh embedder status</button>
      {onContinue && <><button disabled={working || !status?.ready} onClick={() => onContinue(false)}>Continue</button>
        <button disabled={working} onClick={() => { setSkipped(true); onContinue(!status?.ready); }}>Skip for now</button></>}
    </div>
  </section>;
}

/** Read-only line for other screens (Memory): the same runtime receipt, never a separate guess. */
export function EmbedderLine({ invoke, scope }: { invoke: NativeInvoke; scope?: string }) {
  const [status, setStatus] = useState<EmbedderStatus | null>(null);
  useEffect(() => {
    let alive = true, revision = 0;
    const read = () => {
      const current = ++revision;
      void invoke<EmbedderStatus>('runtime_embedder_status').then(
        next => { if (alive && current === revision) setStatus(next); },
        () => { if (alive && current === revision) setStatus(null); }, // remote/older runtimes have their own health surface
      );
    };
    read();
    window.addEventListener('bob:embedder-status', read);
    window.addEventListener('focus', read);
    return () => { alive = false; window.removeEventListener('bob:embedder-status', read); window.removeEventListener('focus', read); };
  }, [invoke, scope]);
  if (!status) return null;
  const truth = embedderTruth(status);
  return <span className={`embedder-line ${truth.ready ? 'good' : 'poor'}`} role="status">
    {truth.text}</span>;
}
