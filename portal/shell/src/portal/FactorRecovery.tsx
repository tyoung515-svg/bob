import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { Catalog, Identity, RecoveryPair, RecoveryAction, RecoveryStatus } from './portal-protocol';
import { operationReason } from './durable-runs';
import { toPortalError } from './portal-protocol';
export const recoveryOwner = (identity?: Identity | null) => identity?.token_type === 'portal' && identity.owner_controls_eligible === true;
export function recoveryReason(catalog: Catalog | null) {
  return ['binding', 'prepare', 'commit', 'cancel'].map(op => operationReason(catalog, 'auth.factors.' + op)).find(Boolean) || operationReason(catalog, 'settings.status', 1);
}
export function FactorRecovery({ client, catalog, onReconnect }: { client: PortalClient; catalog: Catalog | null; onReconnect: () => void }) {
  const [pair, setPair] = useState<RecoveryPair>('password_totp');
  const [printed, setPrinted] = useState(false);
  const [status, setStatus] = useState<RecoveryStatus | null>(null);
  const [busy, setBusy] = useState(true);
  const [nativeReason, setNativeReason] = useState('Checking native recovery support.');
  const [message, setMessage] = useState('');
  const [now, setNow] = useState(Date.now());
  const live = useRef(true);
  const reason = recoveryReason(catalog);
  const statusReason = operationReason(catalog, 'settings.status', 1);
  useEffect(() => {
    live.current = true;
    void Promise.all([client.recoveryCapability(), client.recoveryPending()]).then(([cap, pending]) => {
      if (live.current) { setNativeReason(cap.available ? '' : cap.reason || 'Native recovery is unavailable.'); setStatus(pending); }
    }).catch(() => { if (live.current) setNativeReason('Native recovery support could not be checked.'); }).finally(() => { if (live.current) setBusy(false); });
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { live.current = false; clearInterval(timer); };
  }, [client]);
  function received(next: RecoveryStatus | null) {
    setStatus(next);
    if (next?.phase === 'activated') window.dispatchEvent(new Event('bob-owner-invalidated'));
  }
  async function start() {
    setBusy(true); setMessage('');
    try { const next = await client.recoveryStart(pair, printed); if (live.current) received(next); }
    catch (e) { if (live.current) setMessage('Recovery did not finish: ' + toPortalError(e).code + '. Check the original receipt before starting again.'); }
    finally {
      // Native metadata also recovers a dropped generic IPC reply. Never repeat
      // start automatically; this query is local and contains no factor input.
      try { const next = await client.recoveryPending(); if (live.current) received(next); } catch { if (live.current) setMessage('Outcome unknown. Keep the pending material and check the original receipt.'); }
      if (live.current) setBusy(false);
    }
  }
  async function act(action: RecoveryAction) {
    if (!status) return;
    setBusy(true); setMessage('');
    try { const next = await client.recoveryAct(status.handle, action); if (live.current) received(next); }
    catch (e) { if (live.current) setMessage(toPortalError(e).code + '. Material remains PENDING. No factors were resent. Before commit, old factors remain authoritative.'); }
    finally {
      try { const next = await client.recoveryPending(); if (live.current) received(next); } catch { if (live.current) setMessage('Outcome unknown. Check the original recovery receipt.'); }
      if (live.current) setBusy(false);
    }
  }
  const expired = !!status && status.expires_at * 1000 <= now;
  const prepared = status && ['proof_verified', 'material_saved'].includes(status.phase);
  const terminal = status && ['activated', 'not_changed', 'cancelled'].includes(status.phase);
  return <section className="set-card" aria-label="Two-factor enrollment and recovery">
    <h3>Authenticator and recovery stick</h3>
    <p>Any two of your password, authenticator and recovery stick recover the third. Every recovery replaces the stick and ends all sessions. If two factors are lost, use an administrator at the machine’s console.</p>
    <p>Factor entry and authenticator setup open in a protected Windows window. Keep the replacement file or printout PENDING until its receipt proves activation.</p>
    {reason && <p role="status">{reason}</p>}{nativeReason && <p role="status">{nativeReason}</p>}
    {!status && <fieldset disabled={busy || !!reason || !!nativeReason}><legend>Set up or recover</legend>
      <label>Factors you have <select aria-label="Recovery factors" value={pair} onChange={e => setPair(e.target.value as RecoveryPair)}>
        <option value="setup">First enrollment (provisioning capability)</option>
        <option value="password_totp">Password + authenticator: replace stick</option>
        <option value="password_stick">Password + stick: replace authenticator</option>
        <option value="totp_stick">Authenticator + stick: replace password</option>
      </select></label>
      {['password_stick', 'totp_stick'].includes(pair) && <label><input type="checkbox" aria-label="Use printed recovery code" checked={printed} onChange={e => setPrinted(e.target.checked)} /> My existing recovery code is printed</label>}
      <button disabled={busy || !!reason || !!nativeReason} onClick={() => void start()}>Continue in secure window</button>
    </fieldset>}
    {prepared && <>
      <p role="status">PENDING — {status.saved ? 'Replacement material saved and verified.' : 'Proof verified. Save the replacement material before activation.'}</p>
      {status.material_available === false && <p role="alert">Pending secret material was lost when the app closed. Cancel this challenge and use fresh factor proof. The old factors remain authoritative.</p>}
      {expired && <p role="alert">Challenge expired. Cancel it; old factors remain authoritative. Fresh factor proof is required for another recovery.</p>}
      {['setup', 'password_stick'].includes(status.pair) && <button disabled={busy || expired || status.material_available === false} onClick={() => void act('seed')}>Show new authenticator setup</button>}
      <button disabled={busy || expired || status.material_available === false} onClick={() => void act('usb')}>Save to USB</button>
      <button disabled={busy || expired || status.material_available === false} onClick={() => void act('print')}>Print recovery code</button>
      <p>The print system receives the code. Accept a complete printout in the native window before activation.</p>
      <button disabled={busy || expired || !status.saved || !!operationReason(catalog, 'auth.factors.commit')} title={operationReason(catalog, 'auth.factors.commit')} onClick={() => void act('commit')}>Activate replacement</button>
      <button disabled={busy || !!operationReason(catalog, 'auth.factors.cancel')} title={operationReason(catalog, 'auth.factors.cancel')} onClick={() => void act('cancel')}>Cancel recovery</button>
    </>}
    {status?.phase === 'unknown' && <><p role="status">Outcome unknown. Material is PENDING. Check the original receipt; do not submit factors again.</p>
      <button disabled={busy || !!statusReason} onClick={() => void act('check')}>Check recovery receipt</button>{statusReason && <p>{statusReason}</p>}</>}
    {terminal && <>
      <p role="status">{status.phase === 'activated' ? (status.outcome?.restarted ? 'Replacement activated. The old stick and all sessions are invalid. Reconnect to sign in again.' : 'Replacement activated; readiness failed. The old stick and all sessions remain invalid. Reconcile server readiness before reconnecting.') : 'Recovery not activated. Old factors remain authoritative. Discard the pending output.'}</p>
      <button disabled={busy} onClick={() => void act('finish')}>Done</button>
      {status.phase === 'activated' && <button onClick={onReconnect}>Reconnect after recovery</button>}
    </>}
    {message && <p role="alert">{message}</p>}
  </section>;
}
