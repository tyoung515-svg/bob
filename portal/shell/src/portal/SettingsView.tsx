import { Fragment, useEffect, useState, type ReactNode } from 'react';
import { ExecutionApprovals } from './ExecutionApprovals';
import { operationReason, ownerEligible } from './durable-runs';
import { ModelSetup } from './ModelSetup';
import { EmbedderSetup } from './EmbedderSetup';
import { SelfRepair } from './SelfRepair';
import { PasswordChange } from './PasswordChange';
import { FactorRecovery, recoveryOwner } from './FactorRecovery';
import { ApprovalPolicyEditor, ProviderKeys } from './SettingsControls';
import { pinLabel } from './portal-state';
import type { NativeInvoke } from '../workbench/workbench-client';
import type { PortalClient } from './portal-client';
import { toPortalError, txt } from './portal-protocol';
import type { Catalog, Hello, ProfilesPayload } from './portal-protocol';

export type Appearance = {
  experience: 'simple' | 'pro';
  scale: number;
  accent: string;
  density: 'comfortable' | 'compact';
};

export const DEFAULT_APPEARANCE: Appearance = { experience: 'simple', scale: 100, accent: '#c8dccd', density: 'comfortable' };

const ACCENTS = ['#c8dccd', '#8fd3a5', '#7fb3e6', '#c79be6', '#e9cf95', '#e0a07f', '#f0a6c2'];

type Pane = 'security' | 'appearance' | 'account' | 'keys' | 'models' | 'connections' | 'approvals' | 'data';

const PANES: { id: Pane; label: string }[] = [
  { id: 'appearance', label: 'Appearance' },
  { id: 'account', label: 'Account & sign-in' },
  { id: 'security', label: 'Security' },
  { id: 'keys', label: 'Keys & sign-ins' },
  { id: 'models', label: 'Models' },
  { id: 'connections', label: 'Connection' },
  { id: 'approvals', label: 'Approvals & scope' },
  { id: 'data', label: 'Data on this PC' },
];

type LocalAuth = { name: string; how: string; present: boolean; modified: number | null };

const PASSWORD_STEPS = `read -s -P 'New Bob password: ' pw1; echo
read -s -P 'Type it again: ' pw2; echo
if test -z "$pw1"; or test "$pw1" != "$pw2"; or string match -qr '[\\x22\\x27\\x5c]' -- "$pw1"
    echo 'Not changed: empty, mismatched, or contains a quote or backslash.'
else
    umask 077; mkdir -p ~/.config/bobclaw ~/.config/systemd/user/bobclaw-gateway.service.d
    printf 'BOBCLAW_PASSWORD=%s\\n' "$pw1" > ~/.config/bobclaw/bob-gateway-password.env
    printf '[Service]\\nEnvironmentFile=%%h/.config/bobclaw/bob-gateway-password.env\\n' > ~/.config/systemd/user/bobclaw-gateway.service.d/20-bob-password.conf
    systemctl --user daemon-reload; and systemctl --user restart bobclaw-gateway.service
    sleep 3; systemctl --user is-active bobclaw-gateway.service
end
set -e pw1; set -e pw2`;

const ROLLBACK_STEPS = `rm -f ~/.config/systemd/user/bobclaw-gateway.service.d/20-bob-password.conf ~/.config/bobclaw/bob-gateway-password.env
systemctl --user daemon-reload; and systemctl --user restart bobclaw-gateway.service
systemctl --user is-active bobclaw-gateway.service`;

function Copy({ text }: { text: string }) {
  const [state, setState] = useState('Copy');
  return (
    <button
      type="button"
      onClick={() =>
        void navigator.clipboard?.writeText(text).then(
          () => setState('Copied'),
          () => setState('Copy failed'),
        )
      }
    >
      {state}
    </button>
  );
}

type ConnectionValues = { mode: 'server' | 'this_pc'; lane: string; user: string; host: string; broker: string };
export type ConnectionPayload = { [Field in keyof ConnectionValues]: { value: ConnectionValues[Field]; source: 'env' | 'settings' | 'default' } };

export function serverSshHint(connection: ConnectionPayload | null): string | null {
  const user = connection?.user?.value, host = connection?.host?.value;
  return connection?.mode?.value === 'server' && user && host ? `ssh ${user}@${host}` : null;
}
export function ServerSshHint({ connection }: { connection: ConnectionPayload | null }) {
  const command = serverSshHint(connection);
  return command ? <>Open a terminal on this PC and connect: <code>{command}</code>.</>
    : <>Set the SSH user and host in Settings → Connection before connecting in a terminal.</>;
}

/** The effective connection mode a native connection payload reports; null when it carries none. */
export function connectionMode(payload: unknown): ConnectionValues['mode'] | null {
  const value = (payload as { mode?: { value?: unknown } } | null | undefined)?.mode?.value;
  return value === 'this_pc' || value === 'server' ? value : null;
}

/**
 * What Settings → Connection may list for the effective mode. `transport` is the native
 * `portal_profiles` description and can be older than the saved connection (it is read once at
 * startup, before first-run setup picks This PC), so it is used only when it describes `mode`.
 * SSH host, ssh program and server broker path are returned for `server` mode only.
 */
export function connectionFacts(mode: ConnectionValues['mode'] | null, lane: string, transport: ProfilesPayload['transport'] | null | undefined): [string, string, boolean][] {
  if (!mode) return [];
  const local = mode === 'this_pc';
  const described = !!transport && (transport.mode ? transport.mode === mode : (transport.kind === 'ssh_stdio_jsonl') !== local);
  const rows: [string, string, boolean][] = [['mode', local ? 'This PC' : 'A Bob server', false]];
  if (local) {
    rows.push(['lane', lane || (described && transport.lane) || 'not reported', false]);
    rows.push(['runtime transport', described ? transport.kind : 'not reported', false]);
    // In This PC mode the native payload reuses its `ssh` field for the local runtime launcher.
    if (described && transport.ssh) rows.push(['runtime program', transport.ssh, true]);
    return rows;
  }
  if (!described) return [...rows, ['transport', 'not reported', false]];
  return [...rows, ['transport', `${transport.kind} · ${(transport.targets ?? []).join(' → ')}`, false],
    ['ssh', transport.ssh, true], ['broker', transport.broker, true]];
}

export function ServerConnection({ invoke, serverOnly = false, onSaved, onLoaded }: {
  invoke: NativeInvoke; serverOnly?: boolean; onSaved?: (payload: ConnectionPayload) => void; onLoaded?: (payload: ConnectionPayload) => void;
}) {
  const [connection, setConnection] = useState<ConnectionPayload | null>(null);
  const [values, setValues] = useState<ConnectionValues>({ mode: 'server', lane: '', user: '', host: '', broker: '' });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  function loaded(payload: ConnectionPayload) {
    setConnection(payload);
    setValues({ mode: serverOnly ? 'server' : payload.mode.value, lane: payload.lane.value,
      user: payload.user.value, host: payload.host.value, broker: payload.broker.value });
    onLoaded?.(payload);
  }

  useEffect(() => {
    let alive = true;
    void invoke<ConnectionPayload>('portal_connection_get').then(
      (payload) => { if (alive) loaded(payload); },
      (reason) => { if (alive) setError(toPortalError(reason).detail); },
    );
    return () => { alive = false; };
  }, [invoke]);

  async function save() {
    const required = values.mode === 'server' ? [['user', 'SSH user'], ['host', 'server host'], ['broker', 'broker path']] as const : [];
    const missing = required.filter(([field]) => !values[field].trim()).map(([, label]) => label);
    if (missing.length) { setNote(''); setError(`Enter the missing ${missing.join(', ')}. Ask the person who runs the server for these values.`); return; }
    setBusy(true);
    setNote('');
    setError('');
    try {
      const args = values.mode === 'this_pc'
        ? { mode: values.mode, lane: values.lane }
        : { mode: values.mode, user: values.user, host: values.host, broker: values.broker };
      const payload = await invoke<ConnectionPayload>('portal_connection_set', args);
      loaded(payload);
      onSaved?.(payload);
      setNote('Saved. The next request will reconnect using these settings. Environment values take precedence.');
    } catch (reason) {
      setError(toPortalError(reason).detail);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="set-card" aria-label="Server connection">
      <h3>Server connection</h3>
      {values.mode === 'server' && <p>Enter the SSH user, server host and absolute broker path supplied by the person who runs the server. No server is assumed.</p>}
      <form className="server-connection" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <fieldset disabled={busy}>
          <legend>Connect to</legend>
          {(serverOnly ? [['server', 'A Bob server']] as const : [['this_pc', 'This PC'], ['server', 'A Bob server']] as const).map(([mode, label]) => (
            <label key={mode}>
              <input type="radio" name="connection-mode" value={mode} checked={values.mode === mode}
                onChange={() => { setValues({ ...values, mode }); setNote(''); setError(''); }} />
              {label}
            </label>
          ))}
          <small className="subtle">Source: {connection?.mode.source ?? 'unavailable'}</small>
        </fieldset>
        {(values.mode === 'this_pc' ? [['lane', 'Lane']] as const
          : [['user', 'User'], ['host', 'Host'], ['broker', 'Broker path']] as const).map(([field, label]) => (
          <div className="server-connection-field" key={field}>
            <label htmlFor={`connection-${field}`}>{label}</label>
            <input id={`connection-${field}`} value={values[field]} autoComplete="off" spellCheck={false}
              disabled={busy} onChange={(event) => setValues({ ...values, [field]: event.target.value })} />
            <small className="subtle">Source: {connection?.[field].source ?? 'unavailable'}</small>
          </div>
        ))}
        <div className="row-actions"><button className="primary" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button></div>
      </form>
      {note && <p className="notice" role="status">{note}</p>}
      {error && <p className="caveat" role="alert">{error}</p>}
    </section>
  );
}

export function SettingsView({
  client,
  invoke,
  hello,
  catalog,
  profiles,
  onProfiles,
  onReconnect,
  onConnectionSaved,
  appearance,
  onAppearance,
  pendingApprovals,
  onConfigured,
  modelSetupRequest = 0,
  developerNotes,
}: {
  client: PortalClient;
  invoke: NativeInvoke;
  hello: Hello | null;
  catalog: Catalog | null;
  profiles: ProfilesPayload | null;
  onProfiles: (payload: ProfilesPayload) => void;
  onReconnect: () => void;
  onConnectionSaved?: (payload: ConnectionPayload) => void;
  appearance: Appearance;
  onAppearance: (next: Appearance) => void;
  pendingApprovals: number;
  onConfigured?: () => void | Promise<void>;
  modelSetupRequest?: number;
  developerNotes?: ReactNode;
}) {
  const [pane, setPane] = useState<Pane>('appearance');
  const [auths, setAuths] = useState<LocalAuth[] | null>(null);
  const [profileChoice, setProfileChoice] = useState(profiles?.active ?? 'bob_server');
  const [profileNote, setProfileNote] = useState('');
  // The saved connection as the native layer reports it, and a transport description re-read
  // after it: the `profiles` prop is a startup snapshot and may describe the previous mode.
  const [connection, setConnection] = useState<ConnectionPayload | null>(null);
  useEffect(() => {
    let alive = true;
    void invoke<ConnectionPayload>('portal_connection_get').then(
      (payload) => { if (alive) setConnection(payload); }, () => undefined,
    );
    return () => { alive = false; };
  }, [invoke]);
  const [liveTransport, setLiveTransport] = useState<ProfilesPayload['transport'] | null>(null);
  function connectionLoaded(payload: ConnectionPayload) {
    setConnection(payload);
    void invoke<ProfilesPayload>('portal_profiles').then(
      (fresh) => { if (fresh?.transport && typeof fresh.transport.kind === 'string') setLiveTransport(fresh.transport); },
      () => undefined, // The details below then list only what the connection payload itself carries.
    );
  }
  const transport = liveTransport ?? profiles?.transport;
  const mode = connectionMode(connection) ?? (transport?.mode === 'this_pc' || transport?.mode === 'server' ? transport.mode
    : transport?.kind ? (transport.kind === 'ssh_stdio_jsonl' ? 'server' : 'this_pc') : null);
  const facts = connectionFacts(mode, connection?.lane?.value ?? '', transport);
  const [now, setNow] = useState(Date.now());
  // The slider previews a number while dragged; the window rescales only on release,
  // otherwise the rescale moves the slider out from under the pointer.
  const [scaleDraft, setScaleDraft] = useState<number | null>(null);

  useEffect(() => { if (modelSetupRequest) setPane('models'); }, [modelSetupRequest]);

  useEffect(() => {
    if (pane !== 'keys' || auths) return;
    void invoke<LocalAuth[]>('local_auth_status')
      .then(setAuths)
      .catch(() => setAuths([]));
  }, [pane, auths, invoke]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, []);

  const passwordReason = operationReason(catalog, 'auth.change_password');

  const set = (change: Partial<Appearance>) => onAppearance({ ...appearance, ...change });
  const expires = hello ? Date.parse(hello.identity.expires_at) : NaN;
  const minutesLeft = Number.isFinite(expires) ? Math.round((expires - now) / 60000) : null;

  async function applyProfile() {
    try {
      const payload = await client.selectProfile(profileChoice);
      onProfiles(payload);
      setProfileNote(`Runtime profile is now ${payload.active}.`);
    } catch (e) {
      setProfileNote(toPortalError(e).detail);
    }
  }

  function panel() {
    switch (pane) {
      case 'appearance':
        return (
          <>
            <section className="set-card">
              <h3>Experience</h3>
              <p className="subtle">
                Simple hides technical detail across the app: message sources, routing and audit, identity lines and ids, and notes about how
                lists are built. Pro shows everything.
              </p>
              <div className="seg">
                {(['simple', 'pro'] as const).map((level) => (
                  <button key={level} type="button" aria-pressed={appearance.experience === level} onClick={() => set({ experience: level })}>
                    {level === 'simple' ? 'Simple' : 'Pro'}
                  </button>
                ))}
              </div>
              <div className="set-preview" aria-label="Preview">
                <small className="subtle">Preview</small>
                <article className="msg assistant">
                  <h2>Bob · 09:41</h2>
                  <p className="content">Here is the summary you asked for.</p>
                  <details className="prov">
                    <summary>source</summary>
                    <small className="provenance">bob:conversation:3cc685b9:message:9b221ff8</small>
                  </details>
                </article>
                <p className="pro-only subtle">identity admin · gateway http://127.0.0.1:7836</p>
              </div>
            </section>
            <section className="set-card">
              <h3>
                Interface size · {scaleDraft ?? appearance.scale}%{scaleDraft !== null && scaleDraft !== appearance.scale ? ' (release to apply)' : ''}
              </h3>
              <div className="set-row">
                <button type="button" onClick={() => set({ scale: Math.max(70, appearance.scale - 10) })} aria-label="Smaller">
                  −
                </button>
                <input
                  type="range"
                  min={70}
                  max={150}
                  step={5}
                  value={scaleDraft ?? appearance.scale}
                  aria-label="Interface size"
                  onChange={(e) => setScaleDraft(Number(e.target.value))}
                  onPointerUp={() => {
                    if (scaleDraft !== null) set({ scale: scaleDraft });
                    setScaleDraft(null);
                  }}
                  onKeyUp={() => {
                    if (scaleDraft !== null) set({ scale: scaleDraft });
                    setScaleDraft(null);
                  }}
                  onBlur={() => {
                    if (scaleDraft !== null) set({ scale: scaleDraft });
                    setScaleDraft(null);
                  }}
                />
                <button type="button" onClick={() => set({ scale: Math.min(150, appearance.scale + 10) })} aria-label="Larger">
                  +
                </button>
                <button type="button" className="link" onClick={() => set({ scale: 100 })}>
                  Reset to 100%
                </button>
              </div>
            </section>
            <section className="set-card">
              <h3>Accent colour</h3>
              <div className="set-row">
                {ACCENTS.map((colour) => (
                  <button
                    key={colour}
                    type="button"
                    className={`swatch${appearance.accent === colour ? ' on' : ''}`}
                    style={{ background: colour }}
                    aria-label={`Accent ${colour}`}
                    onClick={() => set({ accent: colour })}
                  />
                ))}
              </div>
            </section>
            <section className="set-card">
              <h3>Density</h3>
              <div className="seg">
                {(['comfortable', 'compact'] as const).map((density) => (
                  <button key={density} type="button" aria-pressed={appearance.density === density} onClick={() => set({ density })}>
                    {density === 'comfortable' ? 'Comfortable' : 'Compact'}
                  </button>
                ))}
              </div>
              <p className="subtle">Compact tightens text and spacing everywhere, this screen included. Theme: dark; a light theme is not built yet.</p>
            </section>
          </>
        );
      case 'security':
        return recoveryOwner(hello?.identity) ? <FactorRecovery key={JSON.stringify([hello?.identity.gateway, hello?.identity.sub])} client={client} catalog={catalog} onReconnect={onReconnect} /> : null;
      case 'account':
        return (
          <>
            <section className="set-card">
              <h3>Signed in</h3>
              <dl className="facts">
                <dt>identity</dt>
                <dd>{hello ? txt(hello.identity.sub) : 'not connected'}</dd>
                <dt>gateway</dt>
                <dd>{hello ? txt(hello.identity.gateway) : '—'}</dd>
                <dt>access token</dt>
                <dd>
                  {minutesLeft === null
                    ? 'expiry not reported'
                    : minutesLeft <= 0
                      ? 'expired — reconnect'
                      : `expires in ${minutesLeft >= 120 ? `${Math.round(minutesLeft / 60)} h` : `${minutesLeft} min`}`}
                </dd>
                <dt>how</dt>
                <dd>This app reaches Bob over your SSH key; the broker on the server holds Bob&apos;s scoped token. The app never sees or stores it.</dd>
              </dl>
              <button type="button" onClick={onReconnect}>
                Reconnect
              </button>
            </section>
            {hello?.identity.token_type === 'portal' && hello.identity.owner_controls_eligible === true && (
              passwordReason ? <section className="set-card" aria-label="Change Bob password in a terminal">
                <h3>Bob sign-in: password and two-factor</h3>
                <p role="status">{passwordReason}</p>
              <p className="caveat">
                Changing it restarts your Bob gateway, which ends open chat streams. Resetting two-factor is not covered here yet.
              </p>
              <details className="set-advanced" open>
                <summary>Advanced: change it in a terminal instead</summary>
                <ol className="set-steps">
                  <li>
                    <ServerSshHint connection={connection} />
                  </li>
                  <li>
                    Paste these lines. They ask for the new password twice at a hidden prompt, refuse an empty or mismatched one, write it to a
                    file only your user can read, point your gateway at it, restart it and print <code>active</code>.
                    <div className="set-code">
                      <pre>{PASSWORD_STEPS}</pre>
                      <Copy text={PASSWORD_STEPS} />
                    </div>
                  </li>
                  <li>
                    Sign in to Bob with the new password and your two-factor code. Only your gateway reads this file; other lanes keep
                    their own shared setting.
                  </li>
                </ol>
                <details>
                  <summary>If it does not come back up: undo</summary>
                  <div className="set-code">
                    <pre>{ROLLBACK_STEPS}</pre>
                    <Copy text={ROLLBACK_STEPS} />
                  </div>
                  <p className="subtle">This returns the gateway to the shared password it used before.</p>
                </details>
              </details>
              </section> : <PasswordChange key={JSON.stringify([hello.identity.gateway, hello.identity.sub])} client={client} catalog={catalog} scope={JSON.stringify([hello.identity.gateway, hello.identity.sub])} />
            )}
          </>
        );
      case 'keys':
        return (
          <>
            <ModelSetup invoke={invoke} catalog={catalog} onConfigured={onConfigured} signinOnly />
            <ProviderKeys invoke={mode === 'this_pc' ? invoke : undefined} key={JSON.stringify([hello?.identity.gateway, hello?.identity.sub])} client={client} catalog={catalog} owner={ownerEligible(hello?.identity, catalog)} scope={JSON.stringify([hello?.identity.gateway, hello?.identity.sub])} />
            <section className="set-card">
              <h3>Windows command-line tool sign-ins</h3>
              <p className="subtle">These show only whether Windows command-line tools have sign-in files on this PC. Bob’s runtime does not use these sign-ins. Checked by file presence only; the files are never opened.</p>
              <ul className="auth-list">
                {(auths ?? []).map((row) => (
                  <li key={row.name}>
                    <span className={`chip ${row.present ? 'ok' : 'off'}`}>{row.present ? 'sign-in file found' : 'not found'}</span>
                    <strong>{row.name}</strong>
                    {row.modified && <small> · updated {new Date(row.modified * 1000).toISOString().slice(0, 10)}</small>}
                    <small className="how">Manage: {row.how}</small>
                  </li>
                ))}
                {auths === null && <li className="subtle">Checking…</li>}
              </ul>
            </section>
          </>
        );
      case 'models':
        return (
          <>
            <ModelSetup invoke={invoke} catalog={catalog} onConfigured={onConfigured} />
            <EmbedderSetup invoke={invoke} />
            <section className="set-card">
              <h3>Faces</h3>
              <p className="subtle">The live registry — read-only.</p>
              <ul className="auth-list">
                {(catalog?.faces ?? []).map((face) => (
                  <li key={face.id}>
                    <span className={`chip ${face.allowed === false ? 'off' : 'ok'}`}>{face.allowed === false ? 'not for this token' : 'usable'}</span>
                    <strong>{face.name}</strong> <small>{face.id}</small>
                  </li>
                ))}
              </ul>
            </section>
            <section className="set-card">
              <h3>Models</h3>
              {catalog && !catalog.models.length && !catalog.backends.length && <p>No models set up yet <button onClick={() => document.querySelector<HTMLElement>('.model-setup button')?.focus()}>Add a provider</button></p>}
              {!catalog && <p>The model catalog is not loaded.</p>}
              <ul className="auth-list">
                {(catalog?.models ?? []).map((model) => (
                  <li key={model.id}>
                    <span className={`chip ${model.available ? 'ok' : 'off'}`}>{model.available ? 'Ready' : 'Set up'}</span>
                    <strong>{pinLabel(model)}</strong>
                    {model.reason && <small className="how">{model.reason}</small>}
                  </li>
                ))}
              </ul>
              {(catalog?.capabilities.warnings ?? []).length > 0 && (
                <p className="caveat">Registry warnings: {(catalog?.capabilities.warnings ?? []).join(' · ')}</p>
              )}
            </section>
          </>
        );
      case 'connections':
        return (
          <>
          <ServerConnection invoke={invoke} onSaved={onConnectionSaved} onLoaded={connectionLoaded} />
            <SelfRepair invoke={invoke} settings onRepaired={onReconnect} />
          <section className="set-card">
            <h3>Connection</h3>
            <label htmlFor="set-profile">Runtime profile</label>
            <div className="set-row">
              <select id="set-profile" value={profileChoice} onChange={(e) => setProfileChoice(e.target.value)}>
                {(profiles?.profiles ?? []).filter((profile) => profiles?.developer_mode || ['bob_server', 'bob_team'].includes(profile.id)).map((profile) => (
                  <option key={profile.id} value={profile.id} disabled={!profile.available}>
                    {profile.label}
                    {profile.available ? '' : ` · unavailable (${profile.reason ?? 'no reason reported'})`}
                  </option>
                ))}
              </select>
              <button type="button" disabled={!profiles || profileChoice === profiles.active} onClick={() => void applyProfile()}>
                Apply
              </button>
              <button type="button" onClick={onReconnect}>
                Reconnect
              </button>
            </div>
            {profileNote && <p className="notice">{profileNote}</p>}
            {(facts.length > 0 || profiles) && (
              <dl className="facts" data-testid="connection-facts">
                {facts.map(([label, value, code]) => (
                  <Fragment key={label}>
                    <dt>{label}</dt>
                    <dd>{code ? <code>{value}</code> : value}</dd>
                  </Fragment>
                ))}
                {profiles && (
                  <>
                    <dt>settings file</dt>
                    <dd>
                      <code>{profiles.settings.path}</code> · {profiles.settings.hardened ? 'user-only permissions' : 'NOT hardened'}
                    </dd>
                  </>
                )}
              </dl>
            )}
            {hello && (
              <dl className="facts">
                <dt>{mode === 'this_pc' ? 'Bob on this PC' : 'server'}</dt>
                <dd>
                  version {txt(hello.server?.version, 'not reported')} · boot {txt(hello.server?.boot, 'not reported')}
                </dd>
              </dl>
            )}
          </section>
          </>
        );
      case 'approvals':
        return (
          <section className="set-card">
            <h3>Approvals & scope</h3>
            <ApprovalPolicyEditor key={JSON.stringify([hello?.identity.gateway, hello?.identity.sub])} client={client} catalog={catalog} owner={ownerEligible(hello?.identity, catalog)} scope={JSON.stringify([hello?.identity.gateway, hello?.identity.sub])} />
            <ExecutionApprovals client={client} catalog={catalog} scope={JSON.stringify([hello?.identity.gateway, hello?.identity.sub])} owner={ownerEligible(hello?.identity, catalog)} />
            <p>
              {pendingApprovals} approval{pendingApprovals === 1 ? '' : 's'} waiting in Needs you. Only high-level items and emergencies should
              land there.
            </p>
            <dl className="facts">
              <dt>may touch</dt>
              <dd>{(hello?.identity.may_touch ?? []).join(', ') || 'not reported'}</dd>
              <dt>may not touch</dt>
              <dd>{(hello?.identity.may_not_touch ?? []).join(', ') || 'not reported'}</dd>
            </dl>
            <p className="subtle">These paths describe this identity's original scope. Approval policy can only restrict that authority further.</p>
          </section>
        );
      case 'data':
        return (
          <section className="set-card">
            <h3>Data kept on this PC</h3>
            <p className="subtle">Things the server has no home for yet. They move to the server at the reconcile.</p>
            <dl className="facts">
              <dt>folder</dt>
              <dd>
                <code>{profiles?.data_root ?? 'not reported'}</code>
              </dd>
              <dt>calendar</dt>
              <dd>calendar.json — Tasks entries</dd>
              <dt>board</dt>
              <dd>board.json — Projects columns</dd>
              <dt>library</dt>
              <dd>library.json — Library apps</dd>
              <dt>sketches</dt>
              <dd>sketches.json — workflow sketches</dd>
              <dt>appearance</dt>
              <dd>this window&apos;s own storage</dd>
            </dl>
            <p className="subtle">No keys, tokens or passwords are stored in any of these.</p>
          </section>
        );
    }
  }

  return (
    <section className="settings" aria-label="Settings">
      <nav className="settings-nav" aria-label="Settings sections">
        <h2>Settings</h2>
        {PANES.filter(row => row.id !== 'security' || recoveryOwner(hello?.identity)).map((row) => (
          <button key={row.id} type="button" aria-pressed={pane === row.id} onClick={() => setPane(row.id)}>
            {row.label}
          </button>
        ))}
      </nav>
      <div className="settings-pane">{panel()}{appearance.experience !== 'simple' && developerNotes}</div>
    </section>
  );
}
