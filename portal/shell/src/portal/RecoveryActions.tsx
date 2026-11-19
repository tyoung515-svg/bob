import { useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';
import type { ProfilesPayload } from './portal-protocol';
import { toPortalError } from './portal-protocol';

export function RecoveryActions({ invoke, onRecovered, onSettings }: {
  invoke: NativeInvoke; onRecovered?: (profiles: ProfilesPayload) => void; onSettings?: () => void;
}) {
  const [error, setError] = useState('');
  async function reset(settings: boolean) {
    if (settings && onSettings) { onSettings(); return; }
    try {
      const profiles = await invoke<ProfilesPayload>('portal_select_profile', { profileId: 'bob_server' });
      if (onRecovered) onRecovered(profiles);
      else window.dispatchEvent(new CustomEvent('bob:recovered', { detail: profiles }));
      if (settings) window.dispatchEvent(new Event('bob:settings'));
    } catch (reason) { setError(toPortalError(reason).detail); }
  }
  return <div className="recovery-actions"><button onClick={() => void reset(false)}>Back to BoB</button><button onClick={() => void reset(true)}>Open Settings</button>{error && <p role="alert">{error}</p>}</div>;
}
