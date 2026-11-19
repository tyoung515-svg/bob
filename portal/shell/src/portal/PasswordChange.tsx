import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { Catalog, PasswordResult } from './portal-protocol';
import { isRateLimited, toPortalError } from './portal-protocol';
import { operationReason } from './durable-runs';
import { useOwnerAction } from './OwnerControls';

export function validPasswordResult(value: unknown): value is PasswordResult {
  if (!value || typeof value !== 'object') return false;
  const result = value as PasswordResult;
  return typeof result.changed === 'boolean' && typeof result.restarted === 'boolean' && (!result.restarted || result.changed);
}
const refusals = new Set(['invalid_password', 'invalid_args', 'bad_request', 'not_dispatched', 'owner_portal_required', 'owner_context_invalid', 'owner_session_invalid', 'owner_session_expired', 'authentication_throttled', 'client_upgrade_required', 'operation_unavailable']);

// Factors live only in uncontrolled inputs and the single native invocation.
// Only a safe lookup identifier survives a failed submission or navigation.
export function PasswordChange({ client, catalog, scope }: { client: PortalClient; catalog: Catalog | null; scope: string }) {
  const current = useRef<HTMLInputElement>(null), next = useRef<HTMLInputElement>(null);
  const again = useRef<HTMLInputElement>(null), totp = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false), live = useRef(true);
  const action = useOwnerAction(client, catalog);
  const storageId = 'bob:password-receipt:' + scope;
  const [lookup, setLookup] = useState(() => {
    try { const value = localStorage.getItem(storageId); return value && /^[A-Za-z0-9_-]{1,128}$/.test(value) ? value : ''; } catch { return ''; }
  });
  const [busy, setBusy] = useState(false), [message, setMessage] = useState('');
  const [loading, setLoading] = useState(true), [uncertain, setUncertain] = useState(false);
  const [changed, setChanged] = useState(false), [signedIn, setSignedIn] = useState(false);
  const [retryAt, setRetryAt] = useState(0), [now, setNow] = useState(Date.now());
  const clear = () => { for (const ref of [current, next, again, totp]) if (ref.current) ref.current.value = ''; };
  useEffect(() => { live.current = true; const timer = setInterval(() => setNow(Date.now()), 1000); return () => { live.current = false; clearInterval(timer); clear(); }; }, []);
  const reason = operationReason(catalog, 'auth.change_password');
  const statusReason = operationReason(catalog, 'settings.status', 1);
  const signInReason = operationReason(catalog, 'grants.list');
  useEffect(() => {
    let active = true; clear(); setLoading(true);
    void client.passwordPending().then(pending => {
      if (!active) return;
      if (pending) { remember(pending.request_key); setUncertain(true); setMessage('Outcome unknown; checking receipt. Credentials were not retried.'); if (!statusReason) void status(pending.request_key); }
    }).catch(e => { if (active) { setUncertain(true); setMessage('Safe pending receipt unavailable (' + toPortalError(e).code + '). Reconnect before changing the password.'); } }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; clear(); };
  }, [client, storageId]);
  function remember(key: string) { setLookup(key); try { localStorage.setItem(storageId, key); } catch { /* retain in memory */ } }
  function forget() { setLookup(''); try { localStorage.removeItem(storageId); } catch { /* optional storage */ } }
  function result(value: unknown) {
    if (!validPasswordResult(value)) throw JSON.stringify({ code: 'invalid_receipt' });
    forget(); setUncertain(false); setChanged(value.changed); setSignedIn(false);
    setMessage(value.changed
      ? value.restarted ? 'Password changed and gateway restarted.' : 'Password changed; gateway restart pending. The new password is authoritative.'
      : 'Password not changed. The current password remains in use. Check the current password and use a fresh authenticator code.');
  }
  async function status(key = lookup) {
    if (!key) return;
    try {
      const reply = await client.passwordStatus({ request_key: key });
      if (!live.current) return;
      if (['ready', 'failed'].includes(reply.phase) && reply.result) result(reply.result);
      else setMessage('Outcome unknown. Checking receipt: ' + reply.phase + '. Credentials were not retried.');
    } catch (e) { if (live.current) setMessage('Outcome unknown. Safe receipt unavailable (' + toPortalError(e).code + '). Check the receipt again; credentials were not retried.'); }
  }
  async function submit() {
    if (inFlight.current || loading || uncertain || reason || lookup || changed || Date.now() < retryAt) { clear(); return; }
    let pw = current.current?.value ?? '', password = next.current?.value ?? '';
    let confirmation = again.current?.value ?? '', code = totp.current?.value ?? '';
    clear();
    if (!pw || !password || password !== confirmation || !/^[0-9]{6}$/.test(code)) { pw = password = confirmation = code = ''; setMessage('Enter the current password, matching new passwords, and a six-digit authenticator code.'); return; }
    confirmation = '';
    inFlight.current = true; setBusy(true); setMessage('');
    try {
      let reply: PasswordResult;
      try { const pending = client.changePassword(pw, password, code); pw = password = code = ''; reply = await pending; }
      finally { pw = password = code = ''; }
      if (live.current) result(reply);
    } catch (e) {
      const error = toPortalError(e);
      if (error.request_key) remember(error.request_key);
      if (!live.current) return;
      if (isRateLimited(error) || refusals.has(error.code)) {
        forget(); setUncertain(false);
        if (error.retry_after) setRetryAt(Date.now() + error.retry_after * 1000);
        setMessage(isRateLimited(error) ? error.detail : 'Password not changed: ' + error.code + (error.retry_after ? ' · Retry-After: ' + error.retry_after + ' seconds.' : '.'));
      } else {
        setUncertain(true); setMessage('Outcome unknown; checking receipt. Credentials were not retried.');
        if (error.request_key && !statusReason) await status(error.request_key);
      }
    } finally { pw = password = confirmation = code = ''; clear(); inFlight.current = false; if (live.current) setBusy(false); }
  }
  async function check() { if (inFlight.current) return; inFlight.current = true; clear(); setBusy(true); try { await status(); } finally { inFlight.current = false; if (live.current) setBusy(false); } }
  async function signIn() {
    setBusy(true);
    try {
      // Reuse Confirm it's you for a real owner read after epoch invalidation.
      await action.run('grants.list', {}, () => client.grantsList(), undefined, 'Sign in again');
      if (live.current) setSignedIn(true);
    } catch (e) { if (live.current) setMessage('Password changed. Sign-in was not completed (' + toPortalError(e).code + ').'); }
    finally { if (live.current) setBusy(false); }
  }
  const disabled = loading || uncertain || busy || !!reason || !!lookup || changed || now < retryAt;
  return <section className="set-card" aria-label="Change Bob password">{action.dialog}
    <h3>Bob sign-in: password and two-factor</h3>
    <p>Enter your current password, a new password twice, and a fresh authenticator code. They are verified once. The fields clear when submitted. Password rules are checked by the server.</p>
    {reason && <p role="status">{reason}</p>}
    <form className="pw-form" onSubmit={e => { e.preventDefault(); void submit(); }}>
      <fieldset disabled={disabled}><legend>Change password</legend>
        <label>Current password<input ref={current} aria-label="Current password" type="password" autoComplete="off" /></label>
        <label>New password<input ref={next} aria-label="New password" type="password" autoComplete="off" /></label>
        <label>New password again<input ref={again} aria-label="New password again" type="password" autoComplete="off" /></label>
        <label>Authenticator code<input ref={totp} aria-label="Password authenticator code" type="password" inputMode="numeric" autoComplete="off" maxLength={6} /></label>
        <button className="primary" disabled={disabled}>{busy ? 'Checking…' : 'Change password'}</button>
      </fieldset>
    </form>
    {message && <p role="status">{message}</p>}
    {lookup && <><button type="button" disabled={busy || !!statusReason} onClick={() => void check()}>Check password receipt</button>{statusReason && <p>{statusReason}</p>}</>}
    {changed && <><p>Other sessions were signed out. This session must sign in again with the new password and a fresh authenticator code.</p>
      {signedIn ? <p role="status">This owner session is signed in again.</p> : <><button type="button" disabled={busy || !!signInReason} onClick={() => void signIn()}>Sign in again</button>{signInReason && <p>{signInReason} Confirm again when using owner controls.</p>}</>}
    </>}
  </section>;
}
