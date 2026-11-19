import { useEffect, useRef, useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';
import { toPortalError } from './portal-protocol';

export type RuntimeUpgradeStatus = {
  installed: { build_id: string }; bundled: { build_id: string };
  available: boolean; running: boolean; blocked?: string;
  upgrade: { id: string; phase: string; outcome: string; reason?: string } | null;
};
const phases: Record<string, string> = {
  preflight: 'Checking the update', backup: 'Backing up runtime and database', stop: 'Stopping BoB services',
  swap: 'Installing runtime files', units: 'Registering services', migrations: 'Updating the database',
  'refresh-env': 'Refreshing lane configuration', hooks: 'Updating runtime profiles', start: 'Starting BoB',
  health: 'Checking BoB health', publish: 'Saving the new version', done: 'Update complete',
  rollback: 'Restoring the previous runtime and database', rolled_back: 'Previous runtime restored',
};
export const UPGRADE_REASONS: Record<string, string> = {
  runtime_too_old: 'This runtime is too old to update in place. Back up your data, remove the old runtime and set up BoB again.',
  runtime_unhealthy: 'The runtime health check did not pass.',
  insufficient_space: 'There is not enough free space to update the runtime.',
  unsupported_layout: 'This runtime has a layout that this update does not support.',
};
export function upgradeReason(reason: string | undefined): string | null {
  return reason && Object.prototype.hasOwnProperty.call(UPGRADE_REASONS, reason) ? UPGRADE_REASONS[reason] : null;
}
const resumable = (status: RuntimeUpgradeStatus | null) => !!status?.upgrade &&
  ['running', 'rolling_back', 'rollback_failed'].includes(status.upgrade.outcome);

/**
 * `status.upgrade` is the runtime's LAST journal entry, so it outlives the app session that ran it.
 * A `completed` entry is only news for the session that started the upgrade or watched it run;
 * `connected` counts this session's successful (re)connects so the note retires once one lands.
 */
export function RuntimeUpgrade({ invoke, scope, onReconnect, reconnecting = false, connected = 0 }: {
  invoke: NativeInvoke; scope: string; onReconnect?: () => void; reconnecting?: boolean; connected?: number;
}) {
  const [status, setStatus] = useState<RuntimeUpgradeStatus | null>(null);
  const [error, setError] = useState(''), [starting, setStarting] = useState(false);
  const alive = useRef(false), busy = useRef(false), seen = useRef(false), generation = useRef(0);
  const pollNow = useRef<() => void>(() => {});
  // Survives the per-identity status reset below: it describes this app session, not one link.
  const watched = useRef<{ id: string | null; ignore: string | null; completedAt: number | null } | null>(null);
  const connectedNow = useRef(connected);
  connectedNow.current = connected;
  /** `stale` is passed only when this session pressed Upgrade: the entry that was already completed before the press. */
  function observe(next: RuntimeUpgradeStatus, stale?: string | null) {
    const entry = next.upgrade, started = stale !== undefined;
    let mine = watched.current;
    if (started || next.running) {
      // The job can be running before its own journal exists: an entry that is already completed
      // (or was completed before the press) is the previous upgrade, never this one.
      const previous = !!entry && (entry.id === stale || (next.running && entry.outcome === 'completed'));
      if (!entry || previous) { if (!mine || started) mine = { id: null, ignore: entry?.id ?? null, completedAt: null }; }
      else if (!mine || mine.id !== entry.id) mine = { id: entry.id, ignore: null, completedAt: null };
    } else if (mine && mine.id === null && entry && entry.id !== mine.ignore) mine.id = entry.id;
    if (mine && entry && mine.id === entry.id && entry.outcome === 'completed' && !next.running && mine.completedAt === null) {
      mine.completedAt = connectedNow.current;
    }
    watched.current = mine;
  }
  useEffect(() => {
    alive.current = true; seen.current = false; generation.current += 1;
    let active = true, timer: ReturnType<typeof setTimeout> | undefined, revision = 0;
    const poll = async () => {
      const current = ++revision;
      if (timer) clearTimeout(timer);
      try {
        const next = await invoke<RuntimeUpgradeStatus>('runtime_upgrade_status');
        if (!active || current !== revision) return;
        seen.current = true; observe(next); setStatus(next); setError('');
        if (next.running) timer = setTimeout(() => void poll(), 1000);
      } catch (reason) {
        // Remote connections and older installers have no local upgrade surface.
        if (active && current === revision && seen.current) setError(toPortalError(reason).detail);
      }
    };
    setStatus(null); setError(''); setStarting(false);
    pollNow.current = () => { void poll(); };
    void poll();
    window.addEventListener('focus', pollNow.current);
    return () => {
      alive.current = false; active = false; generation.current += 1;
      if (timer) clearTimeout(timer);
      window.removeEventListener('focus', pollNow.current);
    };
  }, [invoke, scope]);
  async function upgrade() {
    if (busy.current || status?.blocked === 'runtime_too_old' || status?.running || (!status?.available && !resumable(status))) return;
    const current = generation.current, stale = status?.upgrade?.outcome === 'completed' ? status.upgrade.id : null;
    busy.current = true; setStarting(true); setError('');
    try {
      const next = await invoke<RuntimeUpgradeStatus>('runtime_upgrade_start');
      if (alive.current && current === generation.current) { observe(next, stale); setStatus(next); pollNow.current(); }
    } catch (reason) { if (alive.current && current === generation.current) setError(toPortalError(reason).detail); }
    finally { busy.current = false; if (alive.current && current === generation.current) setStarting(false); }
  }
  if (!status) return null;
  // A completed journal is shown only when this session ran or watched that upgrade, no newer
  // bundle is waiting, and no successful reconnect has happened since it finished.
  const mine = watched.current;
  const completedHere = status.upgrade?.outcome === 'completed' && !status.available && !!mine
    && mine.id === status.upgrade.id && mine.completedAt !== null && connected <= mine.completedAt;
  const journal = status.upgrade?.outcome === 'completed' && !completedHere ? null : status.upgrade;
  const blocked = status.blocked === 'runtime_too_old';
  if (!blocked && !status.available && !status.running && !journal) return null;
  const reason = upgradeReason(journal?.reason);
  const outcome = journal?.outcome, working = starting || status.running;
  return <section className="set-card runtime-upgrade" aria-label="Runtime update">
    <h2>{status.available ? 'BoB update available' : 'BoB runtime update'}</h2>
    <p>Installed: {status.installed.build_id}. Bundled: {status.bundled.build_id}.</p>
    {status.available && !blocked && <p>Upgrade briefly stops BoB services and backs up the runtime and database. Your lanes, projects, memory, models, and credentials are retained.</p>}
    {blocked && <p role="alert">This runtime is too old to update in place. Nothing was changed. To move to this version, back up your data, remove the old runtime and set up BoB again.</p>}
    {journal && <p role="status" aria-live="polite">{phases[journal.phase] ?? journal.phase}</p>}
    {outcome === 'completed' && !working && <><p role="status">BoB updated successfully. Reconnect to continue.</p>
      {onReconnect && <button type="button" className="primary" disabled={reconnecting} onClick={onReconnect}>Reconnect</button>}</>}
    {outcome === 'rolled_back' && !working && !blocked && <p role="alert">{reason ?? 'The update failed.'} The previous runtime was restored.</p>}
    {outcome === 'failed' && !working && !blocked && <p role="alert">{reason ?? 'The update could not be prepared.'} BoB services were preserved.{!reason && ' Retry the upgrade.'}</p>}
    {outcome === 'rollback_failed' && <p role="alert">Rollback needs attention. As root, run bob-runtime upgrade --rollback {journal?.id}. Services may be stopped.</p>}
    {!working && (outcome === 'running' || outcome === 'rolling_back') && <p role="alert">The update was interrupted. Upgrade resumes its saved journal.</p>}
    {working && <><p role="status">{starting ? 'Copying update… Keep this window open until copying finishes.' : 'Update in progress. You can close this window and return later.'}</p><progress aria-label="Runtime upgrade progress" /></>}
    {error && <p role="alert">{error}</p>}
    {!blocked && (status.available || resumable(status)) && <button className="primary" disabled={working} onClick={() => void upgrade()}>Upgrade</button>}
    <button disabled={starting} onClick={() => pollNow.current()}>Refresh update status</button>
  </section>;
}
