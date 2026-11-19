import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { WorkspaceBrowse } from './portal-protocol';
import type { PortalClient } from './portal-client';
import { BuilderPreview } from './BuilderPreview';
import { readWorkspaceBrowse } from './workspace-browse';

const WorkspaceFiles = createContext<{
  projectId: string | null; paths: string[]; selected: string | null;
  listed: (data: WorkspaceBrowse) => void; open: (path: string | null) => void;
}>({ projectId: null, paths: [], selected: null, listed: () => {}, open: () => {} });
export const useWorkspaceFiles = () => useContext(WorkspaceFiles);

/** Only validated, bounded listings can create transcript links. */
export function WorkspaceFileProvider({ projectId, scope = '', client, online = false, completedTurn = '', children }: {
  projectId: string | null; scope?: string; client?: PortalClient; online?: boolean; completedTurn?: string; children: ReactNode;
}) {
  const cacheKey = JSON.stringify([scope, projectId]);
  const [listing, setListing] = useState<{ key: string; directories: Record<string, string[]> }>({ key: cacheKey, directories: {} });
  const [selection, setSelection] = useState<{ key: string; path: string | null }>({ key: cacheKey, path: null });
  const open = useCallback((path: string | null) => setSelection({ key: cacheKey, path }), [cacheKey]);
  const listed = useCallback((data: WorkspaceBrowse) => {
    if (data.project_id !== projectId) return;
    setListing(prior => ({ key: cacheKey, directories: { ...(prior.key === cacheKey ? prior.directories : {}), [data.path]: data.entries.filter(e => e.kind === 'file').map(e => [data.path, e.name].filter(Boolean).join('/')) } }));
  }, [projectId, cacheKey]);
  const view = useRef<{ key: string; client?: PortalClient; initial: boolean; turn: string; generation: number }>({ key: cacheKey, client, initial: false, turn: completedTurn, generation: 0 });
  const mounted = useRef(true);
  // A new view starts one read; existing messages never trigger individual reads.
  if (view.current.key !== cacheKey || view.current.client !== client) {
    view.current = { key: cacheKey, client, initial: false, turn: completedTurn, generation: view.current.generation + 1 };
  }
  useEffect(() => {
    if (!client || !projectId || !online) return;
    const current = view.current;
    if (current.initial && (!completedTurn || current.turn === completedTurn)) return;
    current.initial = true; current.turn = completedTurn;
    const generation = ++current.generation;
    void readWorkspaceBrowse(client, projectId).then(data => {
      if (mounted.current && view.current === current && current.generation === generation) listed(data);
    }, () => {
      if (mounted.current && view.current === current && current.generation === generation) setListing({ key: cacheKey, directories: {} });
      // Linking is optional; failed listings stay silent.
    });
  }, [client, projectId, cacheKey, online, completedTurn, listed]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const value = useMemo(() => ({ projectId, paths: listing.key === cacheKey ? Object.values(listing.directories).flat() : [], selected: selection.key === cacheKey ? selection.path : null, listed, open }), [projectId, listing, selection, cacheKey, listed, open]);
  return <WorkspaceFiles.Provider value={value}>{children}</WorkspaceFiles.Provider>;
}

export function WorkspaceFilePreview({ client, conversationId }: { client: PortalClient; conversationId: string }) {
  const files = useWorkspaceFiles();
  if (!files.selected || !files.paths.includes(files.selected)) return null;
  return <section className="transcript-file-preview" aria-label="File from answer">
    <button type="button" onClick={() => files.open(null)}>Close file</button>
    <BuilderPreview key={files.selected} client={client} conversationId={conversationId} initialPath={files.selected} autoLoad readOnlyPath />
  </section>;
}
