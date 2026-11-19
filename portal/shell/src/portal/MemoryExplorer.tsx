import { ReadFaultText } from './provider-restart';
// Memory (owner request 2026-11-02): a flat file explorer and a 3D brain over
// the server's central memory, health flags in both, and an agent beside it to
// talk about or manage what you pick. Reads only; saving a new memory is the
// one write, and nothing is sent to an agent until the owner presses Send.
import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { NativeInvoke } from '../workbench/workbench-client';
import { EmbedderLine } from './EmbedderSetup';
import { toPortalError, txt } from './portal-protocol';
import type { Hello, MemoryStatus, PortalError } from './portal-protocol';
import { FLAG_LABEL, buildTree, describeAll, expectedTotal, health, loadAll } from './memory';
import type { Flag, MemItem, TreeNode } from './memory';

const MemoryBrain = lazy(() => import('./MemoryBrain'));

/** Lives in PortalApp so a trip to another tab does not re-read all of memory. */
export type MemoryCache = { items: MemItem[] | null; status: MemoryStatus | null; calls: number; at: number; coverage?: MemoryStatus | null; mode?: 'list' | 'search' };

export type MemoryAsk = 'talk' | 'manage';

const KIND_LABEL: Record<string, string> = { family_memory: 'family', bob_event: 'event', bob_fact: 'fact' };
const CATEGORIES = ['note', 'fact', 'preference', 'project'];

export function MemoryExplorer({
  client,
  cache,
  operations,
  onAsk,
  invoke,
}: {
  client: PortalClient;
  cache: MemoryCache;
  operations?: Hello['operations'];
  onAsk: (items: MemItem[], mode: MemoryAsk) => void;
  invoke?: NativeInvoke;
}) {
  const [items, setItems] = useState<MemItem[]>(cache.items ?? []);
  const [status, setStatus] = useState<MemoryStatus | null>(cache.status);
  const [loading, setLoading] = useState<string>('');
  const [error, setError] = useState<PortalError | null>(null);
  const [layout, setLayout] = useState<'flat' | 'brain'>('flat');
  const [folder, setFolder] = useState('');
  const [open, setOpen] = useState<Set<string>>(new Set(['Family memory', 'Bob events']));
  const [filter, setFilter] = useState('');
  const [flagFilter, setFlagFilter] = useState<Flag | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [current, setCurrent] = useState<MemItem | null>(null);
  const [full, setFull] = useState<{ id: string; text: string } | null>(null);
  const [adding, setAdding] = useState<{ content: string; category: string } | null>(null);
  const [message, setMessage] = useState('');
  const [coverage, setCoverage] = useState(cache.coverage);
  const [mode, setMode] = useState(cache.mode);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const load = useCallback(async () => {
    setError(null);
    setLoading('Reading memory status…');
    try {
      const nextStatus = await client.memoryStatus();
      if (!alive.current) return;
      setStatus(nextStatus);
      const expected = expectedTotal(nextStatus);
      const result = await loadAll(client, expected, (count, n) => {
        if (!alive.current) throw new Error('Connection changed');
        setLoading(`Gathering memories… ${count} (${n} reads)`);
      }, operations);
      if (!alive.current) return;
      const { hits, calls, coverage, mode } = result;
      const described = describeAll(hits);
      setItems(described);
      setCoverage(coverage);
      setMode(mode);
      Object.assign(cache, { items: described, status: nextStatus, calls, coverage, mode, at: Date.now() });
      setMessage(`Loaded ${described.length} memories in ${calls} read-only ${mode === 'list' ? 'pages' : 'searches (legacy partial coverage)'}.`);
    } catch (e) {
      if (alive.current) setError(toPortalError(e));
    } finally {
      if (alive.current) setLoading('');
    }
  }, [client, cache, operations]);

  useEffect(() => {
    if (!cache.items) void load();
  }, [cache, load]);

  async function select(item: MemItem) {
    setCurrent(item);
    setFull(null);
    try {
      const read = await client.memoryRead(item.source_id, 12000);
      if (alive.current) setFull({ id: item.source_id, text: typeof read.content === 'string' ? read.content : JSON.stringify(read, null, 2) });
    } catch {
      // The excerpt is still shown; a failed full read is not fatal.
    }
  }

  async function saveNew() {
    if (!adding?.content.trim()) return;
    try {
      await client.memorySave(adding.content.trim(), adding.category);
      setAdding(null);
      setMessage('Saved to central memory. Refresh to see it in the tree.');
    } catch (e) {
      setError(toPortalError(e));
    }
  }

  const stats = health(items, expectedTotal(coverage ?? status));
  const needle = filter.trim().toLowerCase();
  const shown = items.filter(
    (item) =>
      (!folder || item.folder.join('/') === folder || item.folder.join('/').startsWith(`${folder}/`)) &&
      (!flagFilter || item.flags.includes(flagFilter)) &&
      (!needle || `${item.title} ${item.excerpt}`.toLowerCase().includes(needle)),
  );
  const pickedItems = items.filter((item) => picked.has(item.source_id));
  const tree = buildTree(items);

  function togglePick(id: string) {
    setPicked((set) => {
      const next = new Set(set);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function renderTree(node: TreeNode, depth: number) {
    return node.children.map((child) => {
      const expanded = open.has(child.path);
      return (
        <li key={child.path}>
          <button
            type="button"
            className={`mem-folder${folder === child.path ? ' active' : ''}`}
            style={{ paddingLeft: 8 + depth * 12 }}
            onClick={() => {
              setFolder(child.path);
              if (child.children.length)
                setOpen((set) => {
                  const next = new Set(set);
                  if (next.has(child.path) && folder === child.path) next.delete(child.path);
                  else next.add(child.path);
                  return next;
                });
            }}
          >
            <span className="twisty">{child.children.length ? (expanded ? '▾' : '▸') : ''}</span>
            <span className="name">{child.name}</span>
            <small>{child.count}</small>
            {child.flagged > 0 && <b className="flag-count" title={`${child.flagged} flagged`}>{child.flagged}</b>}
          </button>
          {child.children.length > 0 && expanded && <ul>{renderTree(child, depth + 1)}</ul>}
        </li>
      );
    });
  }

  const reader = current && (
    <article className="mem-reader">
      <header>
        <span className={`kind ${current.kind}`}>{KIND_LABEL[current.kind] ?? current.kind}</span>
        <strong>{current.title}</strong>
        <button type="button" className="link" onClick={() => setCurrent(null)}>
          Close
        </button>
      </header>
      {current.flags.length > 0 && (
        <p className="mem-flags">
          {current.flags.map((flag) => (
            <em key={flag} className={`flag ${flag}`}>
              {FLAG_LABEL[flag]}
            </em>
          ))}
        </p>
      )}
      <pre className="mem-text">{full && full.id === current.source_id ? full.text : current.excerpt}</pre>
      <dl className="facts">
        <dt>source</dt>
        <dd>
          <code>{current.source_id}</code>
        </dd>
        <dt>owner</dt>
        <dd>{current.owner}</dd>
        {current.when && (
          <>
            <dt>when</dt>
            <dd>{current.when.slice(0, 16).replace('T', ' ')}</dd>
          </>
        )}
        <dt>provenance</dt>
        <dd>
          <code>{txt(current.provenance)}</code>
        </dd>
      </dl>
      <div className="work-on-it">
        <button type="button" onClick={() => onAsk([current], 'talk')}>
          Talk about it
        </button>
        <button type="button" onClick={() => onAsk([current], 'manage')}>
          Manage with agent
        </button>
        <button type="button" onClick={() => togglePick(current.source_id)}>
          {picked.has(current.source_id) ? 'Unpick' : 'Pick'}
        </button>
      </div>
    </article>
  );

  return (
    <section className="mem" aria-label="Memory explorer">
      <header className="cal-toolbar">
        <h2 className="cal-title">Memory</h2>
        <div className="seg" role="group" aria-label="Memory view">
          <button type="button" aria-pressed={layout === 'flat'} onClick={() => setLayout('flat')}>
            Flat
          </button>
          <button type="button" aria-pressed={layout === 'brain'} onClick={() => setLayout('brain')}>
            Brain
          </button>
        </div>
        <span className="spacer" />
        <input className="board-search" type="search" aria-label="Filter memories" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button type="button" onClick={() => setAdding(adding ? null : { content: '', category: 'note' })}>
          New memory
        </button>
        <button type="button" onClick={() => void load()} disabled={!!loading}>
          Refresh
        </button>
      </header>

      <div className="mem-health" aria-label="Memory health">
        {error || stats.total === 0 ? (
          <span className="score poor" title="No memories were read, so no score can be given">Health unavailable</span>
        ) : (
          <span className={`score ${stats.score >= 90 ? 'good' : stats.score >= 70 ? 'fair' : 'poor'}`} title="Share of memories with no flag">
            Health {stats.score}%
          </span>
        )}
        {invoke && <EmbedderLine invoke={invoke} />}
        <span>
          {stats.total}
          {stats.expected !== null && stats.expected !== stats.total ? ` of ${stats.expected}` : ''} memories
        </span>
        {Object.entries(stats.byKind).map(([kind, n]) => (
          <span key={kind} className={`kind ${kind}`}>
            {n} {KIND_LABEL[kind] ?? kind}
          </span>
        ))}
        {(Object.keys(stats.flags) as Flag[])
          .filter((flag) => stats.flags[flag] > 0)
          .map((flag) => (
            <button
              key={flag}
              type="button"
              className={`flag ${flag}${flagFilter === flag ? ' on' : ''}`}
              onClick={() => setFlagFilter(flagFilter === flag ? null : flag)}
              title="Show only these"
            >
              {stats.flags[flag]} {FLAG_LABEL[flag]}
            </button>
          ))}
        {(coverage || status) && (
          <span className="coverage" title={JSON.stringify(coverage ?? status)}>
            {mode === 'search' ? 'Legacy search · partial coverage' : coverage?.enumeration_complete === true ? 'All records in the inspected sources' : 'Inspected sources'}
            {(coverage ?? status)?.complete_corpus === true ? ' · full corpus' : ' · partial corpus'}
            {Array.isArray(coverage?.sources) ? ` · sources: ${coverage.sources.map((kind) => KIND_LABEL[String(kind)] ?? String(kind)).join(', ')}` : ''}
            {coverage?.lane_database === 'unavailable_or_out_of_scope' ? ' · lane database unavailable or outside scope' : ''}
            {typeof (coverage ?? status)?.unattributed_events_excluded === 'number' ? ` · ${(coverage ?? status)?.unattributed_events_excluded} unattributed events not shown` : ''}
            {Array.isArray(coverage?.excluded) ? ` · excludes: ${coverage.excluded.join(', ')}` : ''}
          </span>
        )}
      </div>

      {(loading || error || adding || pickedItems.length > 0) && (
        <div className="mem-bar">
          {loading && <span className="subtle">{loading}</span>}
          {error && (
            <span role="alert" className="fault-inline">
              <ReadFaultText client={client} error={error} fallback={<><code>{error.code}</code> {error.detail}</>} />
            </span>
          )}
          {adding && (
            <form
              className="mem-add"
              onSubmit={(event) => {
                event.preventDefault();
                void saveNew();
              }}
            >
              <input aria-label="New memory" placeholder="What should Bob remember?" value={adding.content} onChange={(e) => setAdding({ ...adding, content: e.target.value })} />
              <select aria-label="Category" value={adding.category} onChange={(e) => setAdding({ ...adding, category: e.target.value })}>
                {CATEGORIES.map((category) => (
                  <option key={category}>{category}</option>
                ))}
              </select>
              <button className="primary" disabled={!adding.content.trim()}>
                Save
              </button>
            </form>
          )}
          {pickedItems.length > 0 && (
            <span className="picked">
              {pickedItems.length} picked ·{' '}
              <button type="button" onClick={() => onAsk(pickedItems, 'talk')}>
                Talk about them
              </button>{' '}
              <button type="button" onClick={() => onAsk(pickedItems, 'manage')}>
                Manage with agent
              </button>{' '}
              <button type="button" className="link" onClick={() => setPicked(new Set())}>
                Clear
              </button>
            </span>
          )}
        </div>
      )}

      {layout === 'flat' ? (
        <div className="mem-flat">
          <nav className="mem-tree" aria-label="Memory folders">
            <button type="button" className={`mem-folder${folder === '' ? ' active' : ''}`} onClick={() => setFolder('')}>
              <span className="twisty" />
              <span className="name">All memory</span>
              <small>{tree.count}</small>
              {tree.flagged > 0 && <b className="flag-count">{tree.flagged}</b>}
            </button>
            <ul>{renderTree(tree, 0)}</ul>
          </nav>
          <div className="mem-list" role="list">
            {shown.map((item) => (
              <div
                key={item.source_id}
                role="listitem"
                className={`mem-row${current?.source_id === item.source_id ? ' active' : ''}${item.flags.length ? ' flagged' : ''}`}
              >
                <input type="checkbox" aria-label={`Pick ${item.title}`} checked={picked.has(item.source_id)} onChange={() => togglePick(item.source_id)} />
                <button type="button" onClick={() => void select(item)}>
                  <span className={`dot ${item.kind}`} />
                  <span className="title">{item.title}</span>
                  {item.flags.length > 0 && <em className={`flag ${item.flags[0]}`}>{FLAG_LABEL[item.flags[0]]}</em>}
                  <small>{item.when ? item.when.slice(0, 10) : item.folder[item.folder.length - 1]}</small>
                </button>
              </div>
            ))}
            {!shown.length && <p className="subtle ide-pad">{loading ? 'Loading…' : 'Nothing here matches.'}</p>}
          </div>
          <div className="mem-side">{reader || <p className="subtle ide-pad">Pick a memory to read it, talk about it, or manage it with the agent.</p>}</div>
        </div>
      ) : (
        <div className="mem-brain-wrap">
          <Suspense fallback={<p className="subtle ide-pad">Loading the brain view…</p>}>
            <MemoryBrain items={shown} selected={current?.source_id ?? null} onSelect={(item) => void select(item)} />
          </Suspense>
          {reader && <div className="mem-brain-reader">{reader}</div>}
          <div className="mem-legend">
            <span className="dot family_memory" /> family <span className="dot bob_event" /> event <span className="dot bob_fact" /> fact{' '}
            <span className="dot flagged" /> flagged
          </div>
        </div>
      )}
      <p className="cal-message">{message || 'Reads from central memory on the server. Pick memories and hand them to the agent to talk about or manage.'}</p>
    </section>
  );
}
