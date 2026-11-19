import { ReadFaultText } from './provider-restart';
import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { WorkspaceBrowse } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { useWorkspaceFiles } from './WorkspaceFileLinks';
import { readWorkspaceBrowse } from './workspace-browse';
export { validWorkspaceBrowse } from './workspace-browse';

/** Lazy directory reads. Displayed labels never supply a local filesystem root. */
export function WorkspaceBrowser({ client, projectId, onFile, onChooseFolder }: {
  client: PortalClient; projectId: string; onFile?: (path: string) => void; onChooseFolder?: (path: string) => void;
}) {
  const files = useWorkspaceFiles();
  const [path, setPath] = useState('');
  const [data, setData] = useState<WorkspaceBrowse | null>(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const epoch = useRef(0);
  useEffect(() => { setPath(''); setData(null); }, [client, projectId]);
  useEffect(() => {
    const generation = ++epoch.current;
    setData(null); setError(''); setBusy(true);
    void readWorkspaceBrowse(client, projectId, path).then(value => {
      if (generation === epoch.current) { setData(value); files.listed(value); }
    }).catch(e => { if (generation === epoch.current) setError(JSON.stringify(toPortalError(e))); })
      .finally(() => { if (generation === epoch.current) setBusy(false); });
    return () => { epoch.current++; };
  }, [client, projectId, path, refresh]);
  const parts = path ? path.split('/') : [];
  return <section className="workspace-browser" aria-label="Workspace file browser">
    <nav aria-label="Workspace breadcrumbs">
      <button type="button" onClick={() => setPath('')}>{data?.root_label || 'Workspace root'}</button>
      {parts.map((name, i) => <span key={i}> / <button type="button" onClick={() => setPath(parts.slice(0, i + 1).join('/'))}>{name}</button></span>)}
    </nav>
    <label>Workspace path <input aria-label="Workspace folder path" readOnly value={path || '/'} /></label>
    <button type="button" disabled={busy} onClick={() => setRefresh(n => n + 1)}>Refresh files</button>
    {onChooseFolder && <button type="button" disabled={busy || !data} onClick={() => onChooseFolder(path)}>Choose this folder</button>}
    {busy && <p role="status">Reading files…</p>}
    {error && <p role="alert"><ReadFaultText client={client} error={error} /></p>}
    <ul aria-label="Workspace entries">
      {data?.entries.map(e => <li key={e.name}>
        <button type="button" disabled={e.kind === 'symlink' || e.kind === 'file' && !onFile}
          aria-label={`${e.kind}: ${e.name}`} onClick={() => {
            const child = [...parts, e.name].join('/');
            if (e.kind === 'dir') setPath(child); else if (e.kind === 'file') onFile?.(child);
          }}>{e.kind === 'dir' ? '▸ ' : e.kind === 'symlink' ? '↗ ' : ''}{e.name}</button>
        <small>{e.kind === 'file' ? ` ${e.size} bytes` : e.kind === 'symlink' ? ' (link; traversal disabled)' : ''}</small>
      </li>)}
    </ul>
    {data && !data.entries.length && <p>Empty folder.</p>}
    {data?.truncated && <p role="status">Showing the first 1000 entries. This listing is truncated.</p>}
  </section>;
}
