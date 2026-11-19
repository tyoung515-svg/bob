import { useEffect, useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';
import type { EmbedderStatus } from './EmbedderSetup';

export function EmbedderWarning({ invoke, onSetup, scope }: {
  invoke: NativeInvoke; onSetup: () => void; scope: string;
}) {
  const [degraded, setDegraded] = useState(false);
  useEffect(() => {
    let alive = true;
    let revision = 0;
    const check = () => {
      const current = ++revision;
      void invoke<EmbedderStatus>('runtime_embedder_status').then(
        status => { if (alive && current === revision) setDegraded(!status.ready); },
        () => { if (alive && current === revision) setDegraded(false); }, // remote/older runtimes have their own health surface
      );
    };
    setDegraded(false); check();
    window.addEventListener('bob:embedder-status', check);
    window.addEventListener('focus', check);
    return () => {
      alive = false;
      window.removeEventListener('bob:embedder-status', check);
      window.removeEventListener('focus', check);
    };
  }, [invoke, scope]);
  return degraded ? <p className="caveat" role="status">Memory is degraded to keyword-only recall.
    {' '}<button onClick={onSetup}>Set up the embedder</button></p> : null;
}
