import { useEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import type { NativeInvoke } from '../workbench/workbench-client';
import { toPortalError } from './portal-protocol';
import type { Catalog } from './portal-protocol';
import { backendLabel, useModelProviders } from './model-providers';
import { ClaudeSignin } from './ClaudeSignin';
import { MODEL_APPLYING, modelGeneration, modelRestarting, notifyModelChanged, retryModelRead } from './model-reload';

export type ChatGPTState = { signed_in: boolean; selected_model: string | null; last_refresh: string | null; model_selection_required: boolean };
export type ClaudeState = { signed_in: boolean; selected_model: string | null; last_checked: string | null; model_selection_required: boolean };
export type ModelStatus = { claude?: ClaudeState; signin_in_progress?: boolean; lane: string; providers: Record<string, boolean>; default_backend: string | null; ready: boolean; chatgpt?: ChatGPTState };
type ChatGPTModels = ChatGPTState & { ok: boolean; account_default: string | null; models: {slug: string; account_default: boolean; selected: boolean}[]; error?: {code: string; detail: string} };
type SigninError = { code?: string; detail: string };
type ModelEvent = { event: 'device_code'; url: string; code: string } | { event: 'done'; ok: boolean; model_notice?: string } | ({ event: 'error' } & SigninError);
const LABELS: Record<string, string> = { openai_signin: 'OpenAI (ChatGPT)', openai_key: 'OpenAI', anthropic_key: 'Anthropic', deepseek_key: 'DeepSeek', google_key: 'Google', glm_key: 'GLM (Z.ai)', kimi_key: 'Kimi (Moonshot)', qwen_key: 'Qwen (Alibaba Model Studio)' };
const defaultQuestionKey = (lane: string) => 'bob:chatgpt-default-question:v1:' + lane;

export function ModelSetup({ invoke, onContinue, onConfigured, catalog, signinOnly = false }: {
  invoke: NativeInvoke; onContinue?: (skipped: boolean) => void;
  onConfigured?: () => void | Promise<void>; catalog?: Catalog | null; signinOnly?: boolean;
}) {
  const { providers, error: providersError } = useModelProviders(invoke);
  const keyProviders = providers.filter(row => row.method === 'key');
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [busy, setBusy] = useState(false), [login, setLogin] = useState(false);
  const [claudeBusy, setClaudeBusy] = useState(false), [refreshing, setRefreshing] = useState(false);
  const statusFlight = useRef<Promise<ModelStatus | null> | null>(null), modelsFlight = useRef<Promise<void> | null>(null);
  const [error, setError] = useState(''), [message, setMessage] = useState('');
  const [device, setDevice] = useState<{ url: string; code: string } | null>(null);
  const [keyForm, setKeyForm] = useState(false), [provider, setProvider] = useState('openai');
  const [copied, setCopied] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false), [pageState, setPageState] = useState<'' | 'opening' | 'opened' | 'failed'>('');
  const [signinError, setSigninError] = useState<SigninError | null>(null);
  const [models, setModels] = useState<ChatGPTModels | null>(null), [modelsError, setModelsError] = useState('');
  const [askDefault, setAskDefault] = useState(false);
  const [retryConfirmation, setRetryConfirmation] = useState('');
  const statusRead = useRef(0), statusGeneration = useRef(-1), modelsGeneration = useRef(-1);
  const key = useRef<HTMLInputElement>(null), alive = useRef(true), running = useRef(false), off = useRef<(() => void) | null>(null), submitted = useRef(false), cancelled = useRef(false);
  const setting = useRef(false), modelRead = useRef(0), deviceSequence = useRef(0);
  const signedIn = status?.chatgpt?.signed_in ?? status?.providers?.openai_signin ?? false;
  const selectedModel = models?.selected_model ?? status?.chatgpt?.selected_model;
  useEffect(() => {
    setProvider(current => providers.some(row => row.method === 'key' && row.id === current)
      ? current : keyProviders[0]?.id ?? '');
  }, [providers]);
  useEffect(() => {
    alive.current = true; void refresh();
    const changed = () => { void refresh(); };
    window.addEventListener('bob-model-changed', changed);
    return () => {
      window.removeEventListener('bob-model-changed', changed);
      alive.current = false; modelRead.current++; off.current?.(); off.current = null;
      if (submitted.current) void invoke('runtime_model_cancel').catch(() => {});
      if (key.current) key.current.value = '';
    };
  }, [invoke]);
  useEffect(() => {
    setModelsError('');
    if (signedIn) void loadModels(); else setModels(null);
    return () => { modelRead.current++; };
  }, [invoke, signedIn, status?.lane, status?.chatgpt?.selected_model]);
  function refresh(): Promise<ModelStatus | null> {
    const generation = modelGeneration(invoke);
    if (statusFlight.current && statusGeneration.current === generation) return statusFlight.current;
    statusGeneration.current = generation;
    setRefreshing(true);
    const task = readStatus().finally(() => { if (statusFlight.current === task) { statusFlight.current = null; if (alive.current) setRefreshing(false); } });
    statusFlight.current = task; return task;
  }
  async function readStatus() {
    const sequence = ++statusRead.current, generation = modelGeneration(invoke);
    try {
      const next = await retryModelRead(invoke, () => invoke<ModelStatus>('runtime_model_status'));
      if (alive.current && sequence === statusRead.current && generation === modelGeneration(invoke)) {
        setStatus(next); setError('');
        try { setAskDefault(localStorage.getItem(defaultQuestionKey(next.lane)) === 'pending'); } catch { /* Optional continuity. */ }
      }
      return next;
    } catch (reason) { if (alive.current && sequence === statusRead.current && generation === modelGeneration(invoke)) setError(toPortalError(reason).detail); return null; }
  }
  function loadModels(): Promise<void> {
    const generation = modelGeneration(invoke);
    if (modelsFlight.current && modelsGeneration.current === generation) return modelsFlight.current;
    modelsGeneration.current = generation;
    const task = readModels().finally(() => { if (modelsFlight.current === task) modelsFlight.current = null; });
    modelsFlight.current = task; return task;
  }
  async function readModels() {
    const sequence = ++modelRead.current, generation = modelGeneration(invoke);
    try {
      const next = await retryModelRead(invoke, async () => {
        const value = await invoke<ChatGPTModels>('runtime_model_openai_models');
        if (modelRestarting(invoke) && value.error?.code === 'chatgpt_models_unavailable') throw { code: 'transport_unavailable', detail: value.error.detail };
        return value;
      });
      if (!alive.current || sequence !== modelRead.current || generation !== modelGeneration(invoke)) return;
      setModels(next); setModelsError(next.ok ? '' : next.error?.detail ?? 'ChatGPT models could not be read.');
    } catch {
      if (alive.current && sequence === modelRead.current && generation === modelGeneration(invoke)) { setModelsError('ChatGPT models could not be read. Refresh or sign in again.'); }
    }
  }
  async function configured() {
    await notifyModelChanged(invoke, onConfigured);
    await refresh();
  }
  async function applied(confirmation: string, retry = false) {
    setMessage(MODEL_APPLYING); setError(''); setRetryConfirmation('');
    try {
      if (retry && onConfigured) { await onConfigured(); await refresh(); } else await configured();
      if (alive.current) setMessage(confirmation);
      return true;
    } catch (reason) {
      if (alive.current) { setMessage(''); setError(toPortalError(reason).detail); setRetryConfirmation(confirmation); }
      return false;
    }
  }
  async function signIn() {
    if (running.current || setting.current || claudeBusy || status?.signin_in_progress) return;
    running.current = true; submitted.current = false; cancelled.current = false;
    deviceSequence.current++; setPageState(''); setLinkCopied(false); setSigninError(null);
    let failed = false;
    setBusy(true); setLogin(true); setError(''); setMessage('Starting OpenAI sign-in…'); setDevice(null); setCopied(false);
    try {
      const unlisten = await listen<ModelEvent>('runtime://model', ({ payload }) => {
        if (!alive.current || !running.current || cancelled.current) return;
        if (payload.event === 'device_code') { deviceSequence.current++; setPageState(''); setCopied(false); setLinkCopied(false); setDevice({ url: payload.url, code: payload.code }); setMessage('Waiting for you to finish sign-in in your browser…'); }
        if (payload.event === 'error') { failed = true; deviceSequence.current++; setSigninError({code: payload.code, detail: payload.detail || 'Sign-in stopped.'}); setMessage(''); setDevice(null); }
        if (payload.event === 'done' && payload.ok) { deviceSequence.current++; setMessage(MODEL_APPLYING); setDevice(null); }
      });
      if (!alive.current || cancelled.current) { unlisten(); return; }
      off.current = unlisten; submitted.current = true;
      await invoke('runtime_model_openai_login');
      if (cancelled.current || failed) return;
      if (alive.current) {
        setDevice(null);
        const fresh = await applied('OpenAI sign-in saved.');
        const next = await refresh();
        if (fresh) setMessage(next?.chatgpt?.model_selection_required ? 'Signed in. Choose a ChatGPT model to finish setup.' : 'OpenAI sign-in saved.');
        if (alive.current && next) {
          try { localStorage.setItem(defaultQuestionKey(next.lane), 'pending'); } catch { /* Still ask in this view. */ }
          setAskDefault(true); void loadModels();
        }
      }
    } catch (reason) {
      if (alive.current && !cancelled.current && !failed) {
        const failure = toPortalError(reason);
        setSigninError({code: failure.code, detail: failure.detail || 'Sign-in stopped.'}); setMessage(''); setDevice(null);
      }
    } finally {
      deviceSequence.current++;
      off.current?.(); off.current = null; running.current = false; submitted.current = false;
      if (alive.current) { setBusy(false); setLogin(false); void refresh(); }
    }
  }
  async function cancel() {
    cancelled.current = true;
    deviceSequence.current++; setDevice(null); setPageState(''); setSigninError(null); setMessage('The sign-in was cancelled.');
    if (!submitted.current) return;
    try { await invoke('runtime_model_cancel'); }
    catch (reason) { if (alive.current) setError(toPortalError(reason).detail); }
  }
  async function openSigninPage() {
    if (!device || pageState === 'opening') return;
    const sequence = deviceSequence.current;
    setPageState('opening');
    try {
      await invoke('runtime_model_open_url', { url: device.url });
      if (alive.current && sequence === deviceSequence.current) setPageState('opened');
    } catch {
      if (alive.current && sequence === deviceSequence.current) setPageState('failed');
    }
  }
  async function chooseModel(model: string) {
    if (setting.current || busy || claudeBusy || !models?.models.some(row => row.slug === model)) return;
    setting.current = true; modelRead.current++; setBusy(true); setModelsError('');
    try {
      const next = await invoke<ChatGPTModels>('runtime_model_openai_set_model', { model });
      if (!alive.current) return;
      setModels(next);
      if (!next.ok) { setModelsError(next.error?.detail ?? 'The model could not be selected.'); return; }
      await applied('ChatGPT model saved.');
    } catch {
      if (alive.current) { setModelsError('The model may have been saved. Refresh model status before trying again.'); await refresh(); }
    } finally { setting.current = false; if (alive.current) setBusy(false); }
  }
  function keepDefault() {
    if (status) try { localStorage.removeItem(defaultQuestionKey(status.lane)); } catch { /* Optional continuity. */ }
    setAskDefault(false);
  }
  async function saveKey() {
    const field = key.current;
    if (!field || !field.value.trim() || busy || claudeBusy || setting.current || !keyProviders.some(row => row.id === provider)) return;
    setting.current = true; setBusy(true); setError(''); setMessage('');
    try {
      const pending = invoke('runtime_model_set_key', { provider, key: field.value }); field.value = ''; await pending;
      if (alive.current) await applied((keyProviders.find(row => row.id === provider)?.label ?? provider) + ' saved.');
    } catch {
      if (alive.current) setError('API key could not be saved. Check your connection and try again.');
    } finally { setting.current = false; field.value = ''; if (alive.current) setBusy(false); }
  }
  async function setDefault(backend: string) {
    if (setting.current || busy || claudeBusy || !backend || (!(catalog?.backends ?? []).some(row => row.id === backend && row.available) && !(backend === 'openai_codex' && signedIn && selectedModel))) return;
    setting.current = true; setBusy(true); setError(''); setMessage('');
    try {
      await invoke('runtime_model_set_default', { backend });
      if (alive.current) { keepDefault(); await applied('Default saved.'); }
    } catch (reason) { if (alive.current) setError(toPortalError(reason).detail); }
    finally { setting.current = false; if (alive.current) setBusy(false); }
  }
  return <section className="set-card model-setup" aria-label={signinOnly ? 'ChatGPT sign-in' : 'Connect a model'}>
    <h2>{signinOnly ? 'ChatGPT sign-in' : 'Connect a model'}</h2>
    {!signinOnly && (status ? <><p role="status">{status.ready ? 'A model is ready for chat.' : 'Chat needs a model.'} Lane: {status.lane}</p>
      <ul>{Object.entries(status.providers ?? {}).filter(([, present]) => present).map(([id]) => <li key={id}>{LABELS[id] ?? id}: Saved</li>)}</ul>
      {status.default_backend && <p>Default: {backendLabel(status.default_backend, catalog?.backends.find(row => row.id === status.default_backend)?.label)}</p>}
    </> : <p>Chat needs a model. Check model status or connect one below.</p>)}
    {!signinOnly && <h3>ChatGPT sign-in</h3>}
    <p>Use your ChatGPT subscription to connect GPT to Bob.</p>
    <p role="status">{!status ? 'Checking ChatGPT sign-in…' : signedIn ? 'Signed in · ' + (selectedModel ?? 'Choose a model') : 'Not signed in'}</p>
    {status?.signin_in_progress && <p role="status">A sign-in is in progress</p>}
    {status?.chatgpt?.last_refresh && <p className="subtle">Sign-in refreshed: {status.chatgpt.last_refresh}</p>}
    <div className="row-actions">
      {providers.filter(row => row.method === 'signin' && row.id === 'openai').map(row => <button key={row.id} className="primary" disabled={busy || claudeBusy || !!status?.signin_in_progress} onClick={() => void signIn()}>Sign in with {row.label}</button>)}
      {!signinOnly && <button disabled={busy || claudeBusy || !keyProviders.length} onClick={() => setKeyForm(true)}>Use an API key</button>}
      <button disabled={refreshing} onClick={() => void refresh()}>Refresh model status</button>
    </div>
    {signedIn && <div>
      <label htmlFor="chatgpt-model">ChatGPT model</label>
      <select id="chatgpt-model" value={selectedModel ?? ''} disabled={busy || claudeBusy || !!status?.signin_in_progress || !models?.ok} onChange={e => void chooseModel(e.target.value)}>
        <option value="" disabled>Choose a model…</option>
        {selectedModel && !models?.models.some(row => row.slug === selectedModel) && <option value={selectedModel} disabled>{selectedModel} · not in the current list</option>}
        {(models?.models ?? []).map(row => <option key={row.slug} value={row.slug}>{row.slug}{row.account_default ? ' (account default)' : ''}</option>)}
      </select>
      <p>This sets the provider's default model. Pick any of the provider's models per conversation in the chat using Provider, then Model.</p>
      <button disabled={busy} onClick={() => void loadModels()}>Refresh ChatGPT models</button>
      {status?.chatgpt?.model_selection_required && !selectedModel && <p>Choose a model to finish ChatGPT setup.</p>}
      {models?.account_default && <p>Account default: {models.account_default}</p>}
    </div>}
    {modelsError && <p role="alert">{modelsError}</p>}
    {askDefault && signedIn && <div role="group" aria-label="Choose the default for all agents">
      <p>Make GPT the default for all agents, or keep your current default?</p>
      <button className="primary" disabled={busy || !selectedModel} onClick={() => void setDefault('openai_codex')}>Make GPT the default for all agents</button>
      <button disabled={busy} onClick={keepDefault}>Keep current default</button>
    </div>}
    {device && <div className="model-device">
      <p>Enter this code on the sign-in page:</p><strong className="model-device-code" aria-label="Device code">{device.code}</strong>
      <p>Sign-in address: <span className="model-device-url" aria-label="Sign-in address">{device.url}</span></p>
      <div className="row-actions"><button onClick={() => void (navigator.clipboard ? navigator.clipboard.writeText(device.code) : Promise.reject()).then(() => setCopied(true), () => setError('Could not copy the code.'))}>{copied ? 'Copied' : 'Copy code'}</button>
        <button onClick={() => void (navigator.clipboard ? navigator.clipboard.writeText(device.url) : Promise.reject()).then(() => setLinkCopied(true), () => setError('Could not copy the link. Select the sign-in address and copy it.'))}>{linkCopied ? 'Link copied' : 'Copy link'}</button>
        <button disabled={pageState === 'opening'} onClick={() => void openSigninPage()}>Open sign-in page</button></div>
      {pageState === 'opened' && <p role="status">The sign-in page was opened in your default browser.</p>}
      {pageState === 'failed' && <p role="alert">The sign-in page could not be opened from here. Copy the link and open it in your browser.</p>}
      <p>If the sign-in page says device code sign-in is not enabled, turn it on in ChatGPT under Settings, Security, then press Sign in again for a new code.</p>
      <p>The page signs in whichever ChatGPT account your browser is using; check that the account shown at the top of that page is the one with your subscription.</p>
    </div>}
    {message && <p role="status" aria-live="polite">{message}</p>}
    {login && <button onClick={() => void cancel()}>Cancel sign-in</button>}
    <ClaudeSignin invoke={invoke} status={status} refresh={refresh} configured={configured} externallyBusy={busy} onBusy={setClaudeBusy} catalog={catalog} />
    {!signinOnly && keyForm && <form onSubmit={e => { e.preventDefault(); void saveKey(); }}>
      <label htmlFor="model-provider">Provider</label><select id="model-provider" value={provider} disabled={busy} onChange={e => setProvider(e.target.value)}>{keyProviders.map(row => <option key={row.id} value={row.id}>{row.label}</option>)}</select>
      <label htmlFor="model-key">API key</label><input id="model-key" type="password" ref={key} autoComplete="off" required disabled={busy} />
      <button disabled={busy}>Save API key</button>
    </form>}
    {!signinOnly && !!catalog?.backends.length && <div>
      <label htmlFor="model-default">Default backend</label>
      <select id="model-default" value={status?.default_backend ?? ''} disabled={busy || claudeBusy || !status} onChange={e => void setDefault(e.target.value)}>
        <option value="" disabled>Choose a default…</option>
        {status?.default_backend && !catalog.backends.some(row => row.id === status.default_backend) && <option value={status.default_backend} disabled>{status.default_backend} · not in this catalog</option>}
        {catalog.backends.map(row => <option key={row.id} value={row.id} disabled={!row.available}>{backendLabel(row.id, row.label)}{row.available ? ' · Ready' : ' · unavailable' + (row.unavailable_reason ? ' · ' + row.unavailable_reason : '')}</option>)}
      </select>
    </div>}
    {providersError && <p role="alert">{providersError}</p>}
    {error && <p role="alert">{error}</p>}
    {retryConfirmation && <button onClick={() => void applied(retryConfirmation, true)}>Try loading models again</button>}
    {signinError && <p role="alert" data-signin-code={signinError.code}>{signinError.detail}</p>}
    {onContinue && <div className="row-actions"><button className="primary" disabled={busy || claudeBusy || !status?.ready || (signedIn && status?.default_backend === 'openai_codex' && !selectedModel)} onClick={() => onContinue(false)}>Continue</button><button disabled={busy || claudeBusy} onClick={() => onContinue(true)}>Skip for now</button></div>}
  </section>;
}
