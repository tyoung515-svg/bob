import { useEffect, useState } from 'react';
import type { PortalClient } from './portal-client';
import { newApp } from './library';
import type { LibraryStore } from './library';
import { BuilderPreview } from './BuilderPreview';
import { useBuilderRead } from './builder-read-store';
import { builderApprovalIds, builderOutcome } from './BuilderApprovalCard';
import { builderTime, toPortalError } from './portal-protocol';

/** A user click is required. Agent text can propose this action but cannot save. */
export function SaveToLibrary({ client, store, projectId }: { client: PortalClient; store: LibraryStore; projectId: string }) {
  const [note, setNote] = useState(''), [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let alive = true;
    const refresh = () => { void store.apps().then(apps => { if (alive) setSaved(apps.some(a => a.project_id === projectId && a.status === 'ready' && a.launch === 'open_result')); }, () => {}); };
    refresh(); window.addEventListener('bob-library-changed', refresh);
    return () => { alive = false; window.removeEventListener('bob-library-changed', refresh); };
  }, [store, projectId]);
  async function save() {
    setBusy(true); setNote('');
    try {
      const result = await client.builderProjectResult(projectId);
      if (result.project_id !== projectId || !/\.html$/i.test(result.path)) throw new Error('This project has no successful HTML result yet.');
      const apps = await store.apps();
      const project = (await client.projects()).items.find(p => p.id === projectId);
      const prior = apps.find(a => a.project_id === projectId);
      const app = { ...(prior ?? newApp(project?.name ?? 'Builder result')), project_id: projectId, status: 'ready' as const,
        launch: 'open_result' as const, command: '', folder: '' };
      await store.saveApps(prior ? apps.map(a => a.id === prior.id ? app : a) : [app, ...apps]);
      setSaved(true); window.dispatchEvent(new Event('bob-library-changed'));
      setNote('Saved to Library. Open result shows the static in-app preview. Use Open on this PC to run the page’s inline scripts in a confined browser sandbox. No files were copied.');
    } catch (e) { setNote(`Not saved: ${toPortalError(e).detail}`); }
    finally { setBusy(false); }
  }
  return <span><button disabled={busy || saved} onClick={() => void save()}>{saved ? 'Saved to Library' : 'Save to Library'}</button>{note && <p role="status">{note}</p>}</span>;
}

export function BuilderFinishStrip({ client, store, projectId, conversationId, content, scope, refreshKey }: {
  client: PortalClient; store: LibraryStore; projectId: string; conversationId: string; content: string; scope: string; refreshKey?: string;
}) {
  const read = useBuilderRead(client, conversationId, scope, refreshKey);
  const ids = builderApprovalIds(content, conversationId);
  const outcomes = ids.map(id => { const row = read.records.get(id); return row ? builderOutcome(row, row.state === 'pending' && builderTime(row.expires_at) <= Date.now()) : null; });
  const last = outcomes[outcomes.length - 1];
  const pending = outcomes.some(outcome => outcome === 'Waiting for you');
  const failed = last?.includes('ask it to continue');
  const done = !pending && (!outcomes.length || last === 'Done');
  const earlierFailures = outcomes.slice(0, -1).filter(outcome => outcome?.includes('ask it to continue')).length;
  const label = pending ? 'The Builder is waiting for you' : done ? 'Job done'
    : failed ? 'A check or action did not pass' : 'Some steps are not confirmed — nothing will be replayed';
  return <section className="builder-finish" aria-label="Builder task result"><strong role="status">{label}</strong>
    {done && earlierFailures > 0 && <p className="hint">{earlierFailures} earlier {earlierFailures === 1 ? 'check' : 'checks'} did not pass; the Builder carried on. See the steps above.</p>}
    <LibraryResult client={client} projectId={projectId} />
    <SaveToLibrary client={client} store={store} projectId={projectId} />
  </section>;
}

export function LibraryResult({ client, projectId }: { client: PortalClient; projectId: string }) {
  const [result, setResult] = useState<{ conversation_id: string; path: string } | null>(null), [error, setError] = useState('');
  async function open() {
    setError('');setResult(null);
    try { const result = await client.builderProjectResult(projectId); if (result.project_id !== projectId || !/\.html$/i.test(result.path)) throw new Error('Invalid project result');setResult(result); }
    catch (e) { setError(String(e)); }
  }
  return <><button className="primary play" onClick={() => void open()}>Open result</button>
    {result && <BuilderPreview key={`${projectId}:${result.conversation_id}:${result.path}`} client={client} conversationId={result.conversation_id} initialPath={result.path} autoLoad readOnlyPath />}
    {error && <p role="alert">{error}</p>}</>;
}
