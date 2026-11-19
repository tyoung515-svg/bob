import { useEffect, useState } from 'react';
import App from '../App';
import FunctionalApp from '../WorkbenchApp';
import type { NativeInvoke } from '../workbench/workbench-client';
import PortalApp from './PortalApp';
import { PortalBoundary } from './PortalBoundary';
import { RecoveryActions } from './RecoveryActions';
import { createPortalClient } from './portal-client';
import { toPortalError, type ProfilesPayload } from './portal-protocol';
import { DEFAULT_APPEARANCE, SettingsView } from './SettingsView';

export function PortalShell({ invoke }: { invoke: NativeInvoke }) {
  const [profiles, setProfiles] = useState<ProfilesPayload | null>(null);
  const [error, setError] = useState(''), [settings, setSettings] = useState(false), [epoch, setEpoch] = useState(0);
  const [appearance, setAppearance] = useState(DEFAULT_APPEARANCE);
  function apply(payload: ProfilesPayload) { setProfiles(payload); setError(''); setSettings(false); setEpoch((n) => n + 1); }
  useEffect(() => {
    let alive = true;
    void invoke<ProfilesPayload>('portal_profiles').then(async (payload) => {
      // Defence in depth for old native bridges: never enter the retired runtime by accident.
      if (!payload.developer_mode && !['bob_server', 'bob_team'].includes(payload.active)) {
        payload = await invoke<ProfilesPayload>('portal_select_profile', { profileId: 'bob_server' });
        payload = { ...payload, notice: 'A retired runtime profile was reset to BoB.' };
      }
      if (alive) setProfiles(payload);
    }).catch((reason) => { if (alive) setError(toPortalError(reason).detail); });
    const recovered = (event: Event) => apply((event as CustomEvent<ProfilesPayload>).detail);
    const open = () => { setSettings(true); setEpoch((n) => n + 1); };
    window.addEventListener('bob:recovered', recovered); window.addEventListener('bob:settings', open);
    return () => { alive = false; window.removeEventListener('bob:recovered', recovered); window.removeEventListener('bob:settings', open); };
  }, [invoke]);
  const recovery = <RecoveryActions invoke={invoke} onRecovered={apply} onSettings={() => { setSettings(true); setEpoch((n) => n + 1); }} />;
  return <PortalBoundary key={epoch} recovery={recovery}>
    {settings ? <main className="portal"><button onClick={() => setSettings(false)}>Close Settings</button><SettingsView invoke={invoke} client={createPortalClient(invoke)} hello={null} catalog={null} profiles={profiles} onProfiles={apply} onReconnect={() => setSettings(false)} appearance={appearance} onAppearance={setAppearance} pendingApprovals={0} /></main>
      : error ? <main className="portal"><p role="alert">{error}</p>{recovery}</main>
      : !profiles ? <p className="portal-boot" role="status">Starting the native shell…</p>
      : profiles.developer_mode && profiles.developer_fixture ? <App />
      : <>{profiles.notice && <p className="notice" role="status">{profiles.notice}</p>}{profiles.developer_mode && !['bob_server', 'bob_team'].includes(profiles.active) ? <FunctionalApp invoke={invoke} /> : <PortalApp invoke={invoke} profiles={profiles} onProfiles={apply} />}</>}
  </PortalBoundary>;
}
