import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import type { NativeInvoke } from '../workbench/workbench-client';
import type { Catalog } from './portal-protocol';
import type { ModelStatus } from './ModelSetup';
import { toPortalError } from './portal-protocol';
import { MODEL_APPLYING, modelGeneration, retryModelRead } from './model-reload';

type Event = { event: 'signin_link' | 'done' | 'error'; url?: string; ok?: boolean; code?: string; detail?: string };
type Choice = { slug: string; label: string };
const validCode = (value: string) => value.length > 0 && value.length <= 4096 && !/[^A-Za-z0-9._~+/#=%:-]/.test(value);
const codeInstruction = 'Use the code from Claude: at most 4096 ASCII letters, digits or . _ ~ + / # = % : - without spaces.';
export function ClaudeSignin({ invoke, status, refresh, configured, externallyBusy, onBusy, catalog }: {
  invoke: NativeInvoke; status: ModelStatus | null; refresh: () => Promise<ModelStatus | null>;
  configured: () => Promise<void>; externallyBusy: boolean; onBusy: (busy: boolean) => void; catalog?: Catalog | null;
}) {
  const id = useId(), field = useRef<HTMLInputElement | null>(null);
  const codeRef = useCallback((node: HTMLInputElement | null) => { if (!node && field.current) field.current.value = ''; field.current = node; }, []);
  const terminal = useRef(false);
  const alive = useRef(true), running = useRef(false), launched = useRef(false), cancelled = useRef(false), submitted = useRef(false);
  const off = useRef<(() => void) | null>(null), sequence = useRef(0), reading = useRef<Promise<void> | null>(null), changing = useRef(false);
  const [busy, setBusy] = useState(false), [login, setLogin] = useState(false), [checking, setChecking] = useState(false);
  const [url, setUrl] = useState(''), [page, setPage] = useState<'' | 'opening' | 'opened' | 'failed'>(''), [copied, setCopied] = useState(false);
  const [message, setMessage] = useState(''), [error, setError] = useState(''), [reasonCode, setReasonCode] = useState<string | undefined>();
  const [choices, setChoices] = useState<Choice[]>([]), [confirmLogout, setConfirmLogout] = useState(false);
  const [retryConfirmation, setRetryConfirmation] = useState('');
  const choiceRead = useRef(0), choiceGeneration = useRef(-1);
  const signedIn = status?.claude?.signed_in === true, selected = status?.claude?.selected_model;
  const unavailable = catalog?.backends.find(row => row.id === 'claude_code' && !row.available)?.unavailable_reason
    ?? catalog?.models.find(row => row.backend === 'claude_code' && !row.available)?.reason;
  function clearCode() { if (field.current) field.current.value = ''; }
  function working(value: boolean) { setBusy(value); onBusy(value); }
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false; sequence.current++; clearCode(); off.current?.(); off.current = null;
      if (launched.current) void invoke('runtime_model_cancel').catch(() => {});
    };
  }, [invoke]);
  useEffect(() => {
    if (signedIn || status?.claude?.model_selection_required) void loadChoices();
    else setChoices([]);
  }, [signedIn, status?.claude?.model_selection_required, status?.claude?.selected_model, invoke]);
  function loadChoices(): Promise<void> {
    const generation = modelGeneration(invoke);
    if (reading.current && choiceGeneration.current === generation) return reading.current;
    choiceGeneration.current = generation;
    const current = ++choiceRead.current;
    const task = (async () => {
      try {
        const next = await retryModelRead(invoke, () => invoke<{models: Choice[]}>('runtime_model_claude_models'));
        if (alive.current && current === choiceRead.current && generation === modelGeneration(invoke)) setChoices(next.models);
      } catch (e) { if (alive.current && current === choiceRead.current && generation === modelGeneration(invoke)) { clearCode(); setError(toPortalError(e).detail); } }
    })().finally(() => { if (reading.current === task) reading.current = null; });
    reading.current = task; return task;
  }
  async function applied(confirmation: string) {
    setMessage(MODEL_APPLYING); setError(''); setRetryConfirmation('');
    try {
      await configured(); await loadChoices();
      if (alive.current) setMessage(confirmation);
    } catch (reason) {
      if (alive.current) { setMessage(''); setError(toPortalError(reason).detail); setRetryConfirmation(confirmation); }
    }
  }
  async function signIn() {
    if (running.current || changing.current || externallyBusy || status?.signin_in_progress) return;
    running.current = true; launched.current = false; cancelled.current = false; submitted.current = false; terminal.current = false;
    sequence.current++; clearCode(); setUrl(''); setPage(''); setCopied(false); setError(''); setReasonCode(undefined); setChecking(false);
    working(true); setLogin(true); setMessage('Starting Claude sign-in…');
    let failed = false, done = false;
    try {
      const unlisten = await listen<Event>('runtime://model', ({payload}) => {
        if (!alive.current || !running.current || cancelled.current) return;
        if (payload.event === 'signin_link' && !submitted.current) {
          sequence.current++; setUrl(payload.url ?? ''); setPage(''); setCopied(false); setMessage('');
        }
        if (payload.event === 'error') {
          failed = true; terminal.current = true; sequence.current++; clearCode(); setUrl(''); setChecking(false); setMessage('');
          setReasonCode(payload.code); setError(payload.detail || 'Sign-in stopped.');
        }
        if (payload.event === 'done' && payload.ok === true) { done = true; terminal.current = true; sequence.current++; clearCode(); setUrl(''); setChecking(false); }
      });
      if (!alive.current || cancelled.current) { unlisten(); return; }
      off.current = unlisten; launched.current = true;
      await invoke('runtime_model_claude_login');
      if (!alive.current || cancelled.current || failed) return;
      if (!done) { setError('The sign-in was interrupted.'); return; }
      await applied('Claude sign-in saved. Choose a Claude model.');
    } catch (e) {
      if (alive.current && !cancelled.current && !failed) { clearCode(); const failure = toPortalError(e); setReasonCode(failure.code); setError(failure.detail); setMessage(''); }
    } finally {
      sequence.current++; clearCode(); off.current?.(); off.current = null;
      running.current = false; launched.current = false;
      if (alive.current) { setUrl(''); setChecking(false); setLogin(false); working(false); void refresh(); }
    }
  }
  async function submit() {
    if (submitted.current || cancelled.current || !running.current || !url || !field.current) return;
    let code = field.current.value; clearCode();
    if (!validCode(code)) {
      code = ''; setError(codeInstruction); return;
    }
    submitted.current = true; setChecking(true); setError(''); setMessage('Checking the code with Claude…');
    try { const pending = invoke('runtime_model_claude_submit_code', {code}); code = ''; await pending; }
    catch {
      if (alive.current && !cancelled.current && !terminal.current) {
        clearCode(); setError('The code could not be sent. Start sign-in again.'); setMessage('');
        await invoke('runtime_model_cancel').catch(() => {});
      }
    } finally { code = ''; }
  }
  async function cancel() {
    cancelled.current = true; sequence.current++; clearCode(); setUrl(''); setPage(''); setChecking(false); setError(''); setReasonCode(undefined);
    setMessage('The sign-in was cancelled.');
    if (launched.current) await invoke('runtime_model_cancel').catch(() => { if (alive.current) setError('Sign-in cancellation could not be confirmed. Close this pane and check status.'); });
  }
  async function open() {
    if (!url || page === 'opening') return;
    const current = sequence.current; setPage('opening');
    try { await invoke('runtime_model_open_url', {url}); if (alive.current && current === sequence.current) setPage('opened'); }
    catch { if (alive.current && current === sequence.current) { clearCode(); setPage('failed'); } }
  }
  async function choose(model: string) {
    if (changing.current || busy || externallyBusy || !choices.some(row => row.slug === model)) return;
    changing.current = true; working(true); setError('');
    try { await invoke('runtime_model_claude_set_model', {model}); if (alive.current) { await applied('Claude model saved.'); } }
    catch (e) { if (alive.current) { clearCode(); setError(toPortalError(e).detail); } }
    finally { changing.current = false; if (alive.current) working(false); }
  }
  async function logout() {
    if (changing.current || busy || externallyBusy) return;
    changing.current = true; working(true); setConfirmLogout(false); setError('');
    try { await invoke('runtime_model_claude_logout'); if (alive.current) { await applied('Claude signed out.'); } }
    catch (e) { if (alive.current) { clearCode(); setError(toPortalError(e).detail); } }
    finally { changing.current = false; if (alive.current) working(false); }
  }
  return <section className="claude-signin" aria-label="Claude sign-in">
    <h3>Claude sign-in</h3>
    <p role="status">{!status ? 'Checking Claude sign-in…' : signedIn ? 'Claude (subscription): signed in' : 'Claude (subscription): not signed in'}</p>
    {selected && <p>Chosen model: {choices.find(row => row.slug === selected)?.label ?? selected}</p>}
    {status?.signin_in_progress && <p role="status">A sign-in is in progress</p>}
    <button className="primary" disabled={busy || externallyBusy || !!status?.signin_in_progress} onClick={() => void signIn()}>{error && !signedIn ? 'Start again' : 'Sign in with Claude (subscription)'}</button>
    {url && <div className="model-device">
      <p>Open this link, sign in to Claude, and Claude will show you a code. Paste that code here.</p>
      <p>Sign-in address: <span className="model-device-url" aria-label="Claude sign-in address">{url}</span></p>
      <div className="row-actions"><button onClick={() => void (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject()).then(() => { if (alive.current) setCopied(true); }, () => { if (alive.current) { clearCode(); setError('Could not copy the link. Select the sign-in address and copy it.'); } })}>{copied ? 'Link copied' : 'Copy link'}</button><button disabled={page === 'opening'} onClick={() => void open()}>Open sign-in page</button></div>
      {page === 'opened' && <p role="status">The sign-in page was opened in your default browser.</p>}
      {page === 'failed' && <p role="alert">The sign-in page could not be opened from here. Copy the link and open it in your browser.</p>}
      {!checking && <form onSubmit={e => { e.preventDefault(); void submit(); }}>
        <label htmlFor={id}>Code from Claude</label><input id={id} type="password" ref={codeRef} autoComplete="off" autoCapitalize="off" spellCheck={false} onPaste={e => { if (!validCode(e.clipboardData.getData('text'))) { e.preventDefault(); clearCode(); setError(codeInstruction); } }} />
        <button>Submit code</button>
      </form>}
    </div>}
    {message && <p role="status">{message}</p>}
    {error && <p role="alert" data-signin-code={reasonCode}>{error}</p>}
    {retryConfirmation && <button onClick={() => void applied(retryConfirmation)}>Try loading models again</button>}
    {login && <button onClick={() => void cancel()}>Cancel sign-in</button>}
    {(signedIn || status?.claude?.model_selection_required) && <div>
      <label htmlFor={id + '-model'}>Claude model</label><select id={id + '-model'} disabled={busy || externallyBusy || !!status?.signin_in_progress || !choices.length} value={selected ?? ''} onChange={e => void choose(e.target.value)}>
        <option value="" disabled>Choose a model…</option>{choices.map(row => <option value={row.slug} key={row.slug}>{row.label}</option>)}
      </select>
      <p>This sets the provider's default model. Pick any of the provider's models per conversation in the chat using Provider, then Model.</p>
      <p>This is the list the Claude program accepts, not a list read from the account.</p>
      {status?.claude?.model_selection_required && <p>Choose a model to finish Claude setup.</p>}
    </div>}
    {signedIn && <button disabled={busy || externallyBusy || !!status?.signin_in_progress} onClick={() => setConfirmLogout(true)}>Sign out</button>}
    {confirmLogout && <div role="group" aria-label="Confirm Claude sign-out"><p>Sign out of Claude (subscription)?</p><button onClick={() => void logout()}>Confirm sign out</button><button onClick={() => setConfirmLogout(false)}>Keep signed in</button></div>}
    {unavailable && <p className="subtle">{unavailable}</p>}
  </section>;
}
