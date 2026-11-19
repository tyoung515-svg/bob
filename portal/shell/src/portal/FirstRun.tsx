import { useEffect, useMemo, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import type { NativeInvoke } from '../workbench/workbench-client';
import { toPortalError } from './portal-protocol';
import { ServerConnection } from './SettingsView';
import { RecoveryActions } from './RecoveryActions';
import { ModelSetup } from './ModelSetup';
import { EmbedderSetup } from './EmbedderSetup';
import { qrMatrix } from './qr';

type RuntimeStatus = {
  wsl: 'ok' | 'missing' | 'needs_wsl2' | 'error'; distro_installed: boolean;
  firstboot: boolean; lanes: string[]; healthy: boolean; detail: string;
};
type Progress = { step: string; detail: string };
type LaneReceipt = { otpauth: string; core: number; gateway: number };

function AuthenticatorQR({ uri }: { uri: string }) {
  const result = useMemo(() => {
    try { return { matrix: qrMatrix(uri), error: '' }; }
    catch (error) { return { matrix: null, error: String(error) }; }
  }, [uri]);
  if (!result.matrix) return <p role="alert">{result.error}</p>;
  const size = result.matrix.length + 8;
  const path = result.matrix.flatMap((row, y) => row.flatMap((dark, x) => dark ? [`M${x + 4},${y + 4}h1v1h-1z`] : [])).join('');
  return <svg role="img" aria-label="Authenticator QR code" viewBox={`0 0 ${size} ${size}`} width="300" height="300" shapeRendering="crispEdges">
    <rect width={size} height={size} fill="white" /><path d={path} fill="black" />
  </svg>;
}

export function FirstRun({ invoke, onConnect }: { invoke: NativeInvoke; onConnect: (mode?: 'server' | 'this_pc') => void }) {
  const [choice, setChoice] = useState<'pc' | 'server' | null>(null);
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [progress, setProgress] = useState<Progress[]>([]);
  const [receipt, setReceipt] = useState<LaneReceipt | null>(null);
  const [scanned, setScanned] = useState(false);
  const [step, setStep] = useState<'auth' | 'embedder' | 'model' | 'connect'>('auth');
  const [embedderSkipped, setEmbedderSkipped] = useState(false);
  const [skipped, setSkipped] = useState(false);
  // A runtime that already has lanes (the app reinstalled over it): the lane this PC connects to as it is.
  const [existing, setExisting] = useState<string | null>(null);
  const [laneChoice, setLaneChoice] = useState<'existing' | 'new'>('existing');
  const [picked, setPicked] = useState('');
  const password = useRef<HTMLInputElement>(null);
  const confirm = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let alive = true;
    const unlisten = listen<Progress>('runtime://progress', (event) => {
      if (alive) setProgress((steps) => [...steps, event.payload]);
    });
    void unlisten.catch((reason) => { if (alive) setError(toPortalError(reason).detail); });
    return () => { alive = false; void unlisten.then((off) => off(), () => {}); };
  }, []);
  async function check() {
    setBusy(true); setError('');
    try { setStatus(await invoke<RuntimeStatus>('runtime_status')); }
    catch (reason) { setError(toPortalError(reason).detail); }
    finally { setBusy(false); }
  }
  async function install() {
    setBusy(true); setError(''); setProgress([]);
    try { setStatus(await invoke<RuntimeStatus>('runtime_install')); }
    catch (reason) { setStatus(null); setError(toPortalError(reason).detail); }
    finally { setBusy(false); }
  }
  async function addLane(form: HTMLFormElement) {
    setError('');
    const pass = password.current!, confirmation = confirm.current!;
    if (pass.value !== confirmation.value) { pass.value = ''; confirmation.value = ''; setError('Passwords do not match. Please enter them again.'); return; }
    if (Array.from(pass.value).length < 10) { pass.value = ''; confirmation.value = ''; setError('Use a password with at least 10 characters.'); return; }
    const name = (form.elements.namedItem('name') as HTMLInputElement).value;
    if (!/^[a-z][a-z0-9-]{0,31}$/.test(name)) { pass.value = ''; confirmation.value = ''; setError('Your name must follow [a-z][a-z0-9-]{0,31}.'); return; }
    setBusy(true);
    try {
      // Uncontrolled inputs: no password in React state. Clear both immediately after IPC serialization.
      const pending = invoke<LaneReceipt>('runtime_add_lane', { name, password: pass.value });
      pass.value = ''; confirmation.value = '';
      setReceipt(await pending);
    } catch (reason) { setError(toPortalError(reason).detail); }
    finally { pass.value = ''; confirmation.value = ''; setBusy(false); }
  }
  // Only names the runtime itself reported are ever offered; there is no free text on this path.
  const lanes = Array.isArray(status?.lanes) ? status.lanes.filter((lane): lane is string => typeof lane === 'string' && lane.length > 0) : [];
  const chosen = lanes.includes(picked) ? picked : lanes[0] ?? '';
  async function connectExisting() {
    if (!chosen) return;
    setBusy(true); setError('');
    try {
      // The same command and arguments Settings → Connection saves for This PC. It only records
      // which lane this desktop connects to: the lane is neither created nor modified.
      await invoke('portal_connection_set', { mode: 'this_pc', lane: chosen });
      setExisting(chosen); setStep('embedder');
    } catch (reason) { setError(toPortalError(reason).detail); }
    finally { setBusy(false); }
  }
  const ready = !!receipt || !!existing;
  return <main className="portal first-run"><div className="set-body"><section className="set-card" aria-label="Welcome to Bob">
    <h1>Welcome to Bob</h1>
    {!choice && <><p>Choose where Bob will run.</p><div className="row-actions">
      <button className="primary" onClick={() => { setChoice('pc'); void check(); }}>Set up Bob on this PC</button>
      <button onClick={() => setChoice('server')}>Connect to a Bob server</button>
    </div></>}
    {choice === 'server' && <ServerConnection invoke={invoke} serverOnly onSaved={() => onConnect('server')} />}
    {choice === 'pc' && <>
      {busy && <p role="status">{progress.length ? 'Setting up Bob…' : 'Please wait…'}</p>}
      {!!progress.length && <ol aria-label="Installation progress">{progress.map((step, i) => <li key={i}>{step.detail}</li>)}</ol>}
      {!ready && status && <>
        {status.wsl === 'missing' || status.wsl === 'needs_wsl2' ? <>
          <h2>{status.wsl === 'missing' ? 'Install WSL 2' : 'WSL 2 needs an update'}</h2>
          <ol><li>Open a Windows terminal as administrator.</li><li>{status.wsl === 'missing' ? <>Run <code>wsl --install</code>.</> : <>Run <code>wsl --update</code>, then <code>wsl --set-default-version 2</code>.</>}</li><li>Reboot your PC, then return here.</li></ol>
          <button disabled={busy} onClick={() => void check()}>Retry</button>
        </> : status.wsl === 'error' ? <><p role="alert">{status.detail || 'WSL could not be checked.'}</p><button disabled={busy} onClick={() => void check()}>Retry</button></>
        : !status.distro_installed ? <><p>WSL is ready. Install the bundled Bob runtime to continue.</p><button className="primary" disabled={busy} onClick={() => void install()}>Install Bob</button></>
        : !status.firstboot ? <><p>Bob is installed and completing its first boot. {status.detail}</p><button disabled={busy} onClick={() => void check()}>Retry</button></>
        : <>
          {lanes.length > 0 && <>
            <h2>Choose a lane</h2>
            <p>Bob on this PC already has {lanes.length === 1 ? 'a lane' : `${lanes.length} lanes`}. Use an existing lane or create a new one.</p>
            <fieldset className="server-connection" disabled={busy}>
              <legend>Lane</legend>
              {([['existing', 'Use an existing lane'], ['new', 'Create a new lane']] as const).map(([value, label]) => (
                <label key={value}><input type="radio" name="lane-choice" value={value} checked={laneChoice === value}
                  onChange={() => { setLaneChoice(value); setError(''); }} />{label}</label>
              ))}
            </fieldset>
          </>}
          {lanes.length > 0 && laneChoice === 'existing' ? <form className="server-connection" aria-label="Use an existing lane" onSubmit={(event) => { event.preventDefault(); void connectExisting(); }}>
            <div className="server-connection-field"><label htmlFor="existing-lane">Existing lane</label>
              <select id="existing-lane" value={chosen} disabled={busy} onChange={(event) => setPicked(event.target.value)}>
                {lanes.map((lane) => <option key={lane} value={lane}>{lane}</option>)}
              </select>
              <small>Bob connects to this lane as it is. Nothing in the lane is created or changed.</small></div>
            <button className="primary" disabled={busy}>Use this lane</button>
          </form>
          : <form className="server-connection" onSubmit={(event) => { event.preventDefault(); void addLane(event.currentTarget); }}>
          <h2>{lanes.length > 0 ? 'Create a new lane' : 'Create your first lane'}</h2>
          <div className="server-connection-field"><label htmlFor="first-name">Your name</label>
            <input id="first-name" name="name" required pattern="[a-z][a-z0-9\-]{0,31}" maxLength={32} disabled={busy} autoComplete="username" />
            <small>Lowercase: start with a–z, then a–z, 0–9 or hyphen; 1–32 characters.</small></div>
          <div className="server-connection-field"><label htmlFor="first-password">Password</label><input id="first-password" type="password" ref={password} required minLength={10} disabled={busy} autoComplete="new-password" /></div>
          <div className="server-connection-field"><label htmlFor="first-confirm">Confirm password</label><input id="first-confirm" type="password" ref={confirm} required minLength={10} disabled={busy} autoComplete="new-password" /></div>
          <p>Use at least 10 characters.</p><button className="primary" disabled={busy}>Create lane</button>
        </form>}
        </>}
      </>}
      {receipt && step === 'auth' && <><h2>Add Bob to your authenticator</h2><p>Scan this QR code in your authenticator app.</p>
        <AuthenticatorQR uri={receipt.otpauth} />
        <label><input type="checkbox" checked={scanned} onChange={(event) => setScanned(event.target.checked)} />I've scanned it</label>
        <button className="primary" disabled={!scanned} onClick={() => setStep('embedder')}>Continue</button>
      </>}
      {ready && step === 'embedder' && <EmbedderSetup invoke={invoke} onContinue={(skip) => { setEmbedderSkipped(skip); setStep('model'); }} />}
      {embedderSkipped && <p className="caveat" role="status">Memory is degraded to keyword-only recall. Install the embedder later in Settings → Models.</p>}
      {ready && step === 'model' && <><p>Start with ChatGPT sign-in to use your subscription, or use an API key.</p><ModelSetup invoke={invoke} onContinue={(skip) => { setSkipped(skip); setStep('connect'); }} /></>}
      {ready && step === 'connect' && <><h2>Ready to connect</h2>{existing && <p>Bob will connect to the existing lane <strong>{existing}</strong>.</p>}<p>{skipped ? 'Chat needs a model. Connect one later in Settings → Models.' : 'Your model is connected.'}</p><button className="primary" onClick={() => onConnect('this_pc')}>Connect</button></>}
    </>}
    {error && <><p className="caveat" role="alert">{error}</p><RecoveryActions invoke={invoke} /></>}
    {choice && !busy && !ready && <button onClick={() => { setChoice(null); setError(''); setProgress([]); }}>Back</button>}
    {choice === 'pc' && !status && !busy && <button onClick={() => void check()}>Retry</button>}
  </section></div></main>;
}
