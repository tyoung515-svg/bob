// Workspace IDE pane: explorer, Monaco editor tabs and real terminals over a
// folder on this PC. Loaded lazily so chat never pays for Monaco or xterm.
import { useCallback, useEffect, useImperativeHandle, useRef, useState, forwardRef } from 'react';
import type { ReactNode } from 'react';
import { listen } from '@tauri-apps/api/event';
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api';
import 'monaco-editor/esm/vs/editor/edcore.main';
import 'monaco-editor/esm/vs/basic-languages/monaco.contribution';
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { TERM_EVENT, errorText, readServerText } from './ide-client';
import type { IdeClient, IdeEntry, IdeGit, TermFrame } from './ide-client';
import type { Hello, ServerRoot } from './portal-protocol';

(self as unknown as { MonacoEnvironment: monaco.Environment }).MonacoEnvironment = { getWorker: () => new EditorWorker() };

// Server proposals arrive as unified diff text; colour it the way a diff view would.
if (!monaco.languages.getLanguages().some((lang) => lang.id === 'udiff')) {
  monaco.languages.register({ id: 'udiff' });
  monaco.languages.setMonarchTokensProvider('udiff', {
    tokenizer: {
      root: [
        [/^(\+\+\+|---).*$/, 'comment'],
        [/^@@.*$/, 'keyword'],
        [/^\+.*$/, 'string'],
        [/^-.*$/, 'invalid'],
        [/^diff .*$/, 'type'],
      ],
    },
  });
}

type Tab = { id: string; title: string; kind: 'file' | 'diff' | 'head' | 'server'; path?: string; dirty: boolean; note?: string };

/** What the agent chat may attach: the open file, or just the selected lines. */
export type IdeContext = { path: string; content: string; selection: string; root: string | null };

export type IdeHandle = { openDiff: (title: string, diff: string) => void; context: () => IdeContext | null };

const RECENT_KEY = 'bobportal.ide.recent';

function readRecent(): string[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RECENT_KEY) ?? '[]') as unknown;
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string').slice(0, 8) : [];
  } catch {
    return [];
  }
}

function baseName(path: string): string {
  return path.replace(/\/+$/, '').split('/').pop() || path;
}

function TerminalView({ client, id, active }: { client: IdeClient; id: string; active: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const fit = useRef<FitAddon | null>(null);
  useEffect(() => {
    const term = new Terminal({
      fontFamily: '"Cascadia Code", "DejaVu Sans Mono", monospace',
      fontSize: 13,
      cursorBlink: true,
      theme: { background: '#121417', foreground: '#edece8', cursor: '#c8dccd', selectionBackground: '#34413a' },
    });
    const addon = new FitAddon();
    term.loadAddon(addon);
    fit.current = addon;
    term.open(host.current as HTMLDivElement);
    addon.fit();
    void client.termResize(id, term.cols, term.rows).catch(() => undefined);
    const typed = term.onData((data) => void client.termWrite(id, data).catch(() => undefined));
    const sized = term.onResize(({ cols, rows }) => void client.termResize(id, cols, rows).catch(() => undefined));
    let unlisten: (() => void) | undefined;
    let gone = false;
    void listen<TermFrame>(TERM_EVENT, (event) => {
      if (event.payload.id !== id) return;
      if (event.payload.data) term.write(event.payload.data);
      if (event.payload.exit) term.write('\r\n\x1b[2m[shell exited]\x1b[0m\r\n');
    }).then((fn) => (gone ? fn() : (unlisten = fn)));
    const observer = new ResizeObserver(() => {
      if (host.current?.offsetParent) addon.fit();
    });
    observer.observe(host.current as HTMLDivElement);
    return () => {
      gone = true;
      unlisten?.();
      observer.disconnect();
      typed.dispose();
      sized.dispose();
      term.dispose();
    };
  }, [client, id]);
  useEffect(() => {
    if (active) requestAnimationFrame(() => fit.current?.fit());
  }, [active]);
  return <div ref={host} className="ide-term" hidden={!active} />;
}

type IdeProps = {
  client: IdeClient;
  generation?: number;
  operations?: Hello['operations'];
  /** Live mini terminals for agent turns. */
  agents?: ReactNode;
  /** Bumped when an agent starts working, so its terminal comes into view. */
  agentsPing?: number;
  /** Server tool jobs (proposals with diffs). */
  jobs?: ReactNode;
  onContext?: (path: string | null) => void;
  /** A folder another view asked to open (the Library's "Open in Workspace"). */
  openRequest?: { path: string; n: number } | null;
};

const IdeEditor = forwardRef<IdeHandle, IdeProps>(function IdeEditor({ client, generation = 0, operations, agents, agentsPing = 0, jobs, onContext, openRequest }, ref) {
  const [source, setSource] = useState<'pc' | 'server'>('pc');
  const [serverRoots, setServerRoots] = useState<ServerRoot[]>([]);
  const [serverRoot, setServerRoot] = useState<ServerRoot | null>(null);
  const [serverChildren, setServerChildren] = useState<Record<string, IdeEntry[]>>({});
  const [serverExpanded, setServerExpanded] = useState<Set<string>>(new Set());
  const [serverMessage, setServerMessage] = useState('');
  const serverGeneration = useRef(generation);
  serverGeneration.current = generation;
  const serverAlive = useRef(true);
  const serverAttachment = useRef<string | null>(null);
  const serverAvailable = ['files.roots', 'files.list', 'files.read'].every((op) => operations?.[op]?.implemented && operations[op].readable);
  const [root, setRoot] = useState<string | null>(null);
  const [recent, setRecent] = useState<string[]>(readRecent);
  const [typedPath, setTypedPath] = useState('');
  const [children, setChildren] = useState<Record<string, IdeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [git, setGit] = useState<IdeGit | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [terms, setTerms] = useState<{ id: string; title: string }[]>([]);
  const [activeTerm, setActiveTerm] = useState<string | null>(null);
  const [termOpen, setTermOpen] = useState(true);
  const [panelTab, setPanelTab] = useState<'term' | 'agents'>('term');
  useEffect(() => {
    if (!agentsPing) return;
    setPanelTab('agents');
    setTermOpen(true);
  }, [agentsPing]);
  const [message, setMessage] = useState('');
  const [cursor, setCursor] = useState('');
  // A dirty tab closes on the second click of its ×, so no browser dialog is needed.
  const [closeArmed, setCloseArmed] = useState<string | null>(null);

  const editorHost = useRef<HTMLDivElement>(null);
  const diffHost = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const diffEditor = useRef<monaco.editor.IStandaloneDiffEditor | null>(null);
  const models = useRef(new Map<string, monaco.editor.ITextModel>());
  const originals = useRef(new Map<string, monaco.editor.ITextModel>());
  const saved = useRef(new Map<string, number>());
  const tabsRef = useRef<Tab[]>([]);
  tabsRef.current = tabs;
  const activeTabRef = useRef<string | null>(null);
  activeTabRef.current = activeTab;

  useEffect(() => {
    serverAttachment.current = null;
    setServerRoot(null);
    setServerRoots([]);
    setServerChildren({});
    setServerExpanded(new Set());
    setTabs((list) => list.filter((tab) => tab.kind !== 'server'));
    if (activeTabRef.current?.startsWith('server:')) {
      setActiveTab(null);
      editor.current?.setModel(null);
    }
    for (const [id, model] of models.current) if (id.startsWith('server:')) {
      model.dispose(); models.current.delete(id);
    }
  }, [generation, serverAvailable]);

  useEffect(() => {
    if (source !== 'server') return;
    let alive = true;
    setServerMessage(serverAvailable ? 'Reading server sources…' : 'Server files: not available on this server.');
    if (serverAvailable) void client.serverRoots().then((result) => {
      if (!alive) return;
      setServerRoots(result.items);
      setServerMessage(result.items.length ? '' : 'No server project folders are registered for this account.');
    }).catch((error) => { if (alive) setServerMessage(errorText(error)); });
    return () => { alive = false; };
  }, [client, source, generation, serverAvailable]);

  useEffect(() => {
    serverAlive.current = true;
    const common = {
      theme: 'vs-dark',
      automaticLayout: true,
      fontFamily: '"Cascadia Code", "DejaVu Sans Mono", monospace',
      fontSize: 13,
      minimap: { enabled: true },
      scrollBeyondLastLine: false,
    };
    editor.current = monaco.editor.create(editorHost.current as HTMLDivElement, { ...common, model: null });
    diffEditor.current = monaco.editor.createDiffEditor(diffHost.current as HTMLDivElement, { ...common, readOnly: false, renderSideBySide: true });
    const moved = editor.current.onDidChangeCursorPosition((event) =>
      setCursor(`Ln ${event.position.lineNumber}, Col ${event.position.column}`),
    );
    return () => {
      serverAlive.current = false;
      moved.dispose();
      editor.current?.dispose();
      diffEditor.current?.dispose();
      for (const model of [...models.current.values(), ...originals.current.values()]) model.dispose();
    };
  }, []);

  const listDir = useCallback(
    async (path: string) => {
      try {
        const entries = await client.listDir(path);
        setChildren((all) => ({ ...all, [path]: entries }));
      } catch (error) {
        setMessage(errorText(error));
      }
    },
    [client],
  );

  const refreshGit = useCallback(
    async (path: string) => {
      try {
        setGit(await client.gitStatus(path));
      } catch {
        setGit(null);
      }
    },
    [client],
  );

  async function openFolder(path: string) {
    setSource('pc');
    const clean = path.trim().replace(/\/+$/, '') || '/';
    setMessage('');
    try {
      const entries = await client.listDir(clean);
      setRoot(clean);
      setChildren({ [clean]: entries });
      setExpanded(new Set([clean]));
      void refreshGit(clean);
      const next = [clean, ...recent.filter((item) => item !== clean)].slice(0, 8);
      setRecent(next);
      try {
        window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
      } catch {
        // Recent folders are a convenience only.
      }
      if (!terms.length) void newTerminal(clean);
    } catch (error) {
      setMessage(errorText(error));
    }
  }

  useEffect(() => {
    if (openRequest?.path) void openFolder(openRequest.path);
    // Only a new request should open a folder, not a re-render.
  }, [openRequest?.n]);

  async function pickFolder() {
    try {
      const chosen = await client.pickFolder();
      if (chosen) await openFolder(chosen);
    } catch (error) {
      setMessage(errorText(error));
    }
  }

  async function newTerminal(cwd = root) {
    if (!cwd) return;
    try {
      const id = await client.termOpen(cwd, 100, 24);
      setTerms((list) => [...list, { id, title: `${baseName(cwd)} ${list.length + 1}` }]);
      setActiveTerm(id);
      setTermOpen(true);
    } catch (error) {
      setMessage(errorText(error));
    }
  }

  function closeTerminal(id: string) {
    void client.termClose(id).catch(() => undefined);
    setTerms((list) => {
      const next = list.filter((item) => item.id !== id);
      setActiveTerm((current) => (current === id ? (next[next.length - 1]?.id ?? null) : current));
      return next;
    });
  }

  function show(tab: Tab) {
    setActiveTab(tab.id);
    if (tab.kind === 'head') {
      const original = originals.current.get(tab.id);
      const modified = models.current.get(`file:${tab.path}`);
      if (original && modified) diffEditor.current?.setModel({ original, modified });
    } else {
      editor.current?.updateOptions({ readOnly: tab.kind === 'server' || tab.kind === 'diff' });
      editor.current?.setModel(models.current.get(tab.id) ?? null);
      editor.current?.focus();
    }
  }

  function addTab(tab: Tab) {
    setTabs((list) => (list.some((item) => item.id === tab.id) ? list : [...list, tab]));
    show(tab);
  }

  async function openFile(path: string) {
    const id = `file:${path}`;
    const existing = tabsRef.current.find((tab) => tab.id === id);
    if (existing) return show(existing);
    try {
      const file = await client.readFile(path);
      if (file.binary || file.too_large) {
        setMessage(`${baseName(path)}: ${file.binary ? 'binary file' : 'larger than 4 MB'} — not opened in the editor.`);
        return;
      }
      const model = monaco.editor.createModel(file.content, undefined, monaco.Uri.file(path));
      models.current.set(id, model);
      saved.current.set(id, model.getAlternativeVersionId());
      model.onDidChangeContent(() => {
        const dirty = model.getAlternativeVersionId() !== saved.current.get(id);
        setTabs((list) => list.map((tab) => (tab.id === id && tab.dirty !== dirty ? { ...tab, dirty } : tab)));
      });
      addTab({ id, title: baseName(path), kind: 'file', path, dirty: false });
    } catch (error) {
      setMessage(errorText(error));
    }
  }

  async function openHeadDiff(path: string) {
    await openFile(path);
    const id = `head:${path}`;
    try {
      const head = await client.gitHead(path);
      if (head === null) {
        setMessage(`${baseName(path)} is not committed yet, so there is nothing to compare.`);
        return;
      }
      originals.current.get(id)?.dispose();
      originals.current.set(id, monaco.editor.createModel(head, undefined, monaco.Uri.parse(`git-head:${path}`)));
      const tab: Tab = { id, title: `${baseName(path)} ↔ HEAD`, kind: 'head', path, dirty: false };
      setTabs((list) => (list.some((item) => item.id === id) ? list : [...list, tab]));
      show(tab);
    } catch (error) {
      setMessage(errorText(error));
    }
  }

  const save = useCallback(
    async (tabId: string | null) => {
      const tab = tabsRef.current.find((item) => item.id === tabId);
      const fileId = tab?.kind === 'head' ? `file:${tab.path}` : tab?.id;
      const model = fileId ? models.current.get(fileId) : undefined;
      if (tab?.kind === 'server' || tab?.kind === 'diff') return;
      if (!tab?.path || !model || !fileId) return;
      try {
        await client.writeFile(tab.path, model.getValue());
        saved.current.set(fileId, model.getAlternativeVersionId());
        setTabs((list) => list.map((item) => (item.id === fileId ? { ...item, dirty: false } : item)));
        setMessage(`Saved ${tab.path}`);
        if (root) void refreshGit(root);
      } catch (error) {
        setMessage(errorText(error));
      }
    },
    [client, root, refreshGit],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void save(activeTab);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [save, activeTab]);

  function closeTab(id: string) {
    const tab = tabsRef.current.find((item) => item.id === id);
    if (tab?.dirty && closeArmed !== id) {
      setCloseArmed(id);
      setMessage(`${tab.title} has unsaved changes: press × again to discard them, or Ctrl+S to save.`);
      return;
    }
    setCloseArmed(null);
    const next = tabsRef.current.filter((item) => item.id !== id);
    setTabs(next);
    if (tab?.kind !== 'file' || !next.some((item) => item.kind === 'head' && item.path === tab.path)) {
      models.current.get(id)?.dispose();
      models.current.delete(id);
    }
    originals.current.get(id)?.dispose();
    originals.current.delete(id);
    if (activeTab === id) {
      const last = next[next.length - 1];
      if (last) show(last);
      else {
        setActiveTab(null);
        editor.current?.setModel(null);
      }
    }
  }

  useImperativeHandle(ref, () => ({
    openDiff(title: string, diff: string) {
      const id = `diff:${title}`;
      models.current.get(id)?.dispose();
      models.current.set(id, monaco.editor.createModel(diff, 'udiff'));
      addTab({ id, title, kind: 'diff', dirty: false, note: 'Read-only view of a server proposal. Approve or deny it under Agents › Server jobs.' });
    },
    context() {
      const tab = tabsRef.current.find((item) => item.id === activeTabRef.current);
      if (tab?.kind === 'server') return null;
      if (!tab?.path) return null;
      const model = models.current.get(`file:${tab.path}`);
      if (!model) return null;
      const range = editor.current?.getSelection();
      const selection = tab.kind === 'file' && range && !range.isEmpty() ? model.getValueInRange(range) : '';
      return { path: tab.path, content: model.getValue(), selection, root };
    },
  }));

  async function listServer(path: string, attachment = serverRoot?.attachment_id) {
    if (!attachment || !serverAvailable) return;
    const epoch = generation;
    const entries: IdeEntry[] = [];
    const cursors = new Set<string>();
    let cursor: string | undefined;
    try {
      do {
        const page = await client.serverList(attachment, path, cursor);
        if (!serverAlive.current || epoch !== serverGeneration.current || attachment !== serverAttachment.current) return;
        entries.push(...page.items);
        cursor = page.next_cursor ?? undefined;
        if (cursor && (cursors.has(cursor) || entries.length >= 10000)) throw new Error('Server directory paging limit reached.');
        if (cursor) cursors.add(cursor);
      } while (cursor);
      setServerChildren((all) => ({ ...all, [path]: entries }));
    } catch (error) {
      if (serverAlive.current && epoch === serverGeneration.current && attachment === serverAttachment.current) setServerMessage(errorText(error));
    }
  }

  function chooseServerRoot(attachment: string) {
    const selected = serverRoots.find((item) => item.attachment_id === attachment) ?? null;
    serverAttachment.current = selected?.attachment_id ?? null;
    setServerRoot(selected);
    setServerChildren({});
    setServerExpanded(new Set(['']));
    setServerMessage('');
    if (selected) void listServer('', selected.attachment_id);
  }

  async function openServerFile(path: string) {
    if (!serverRoot || !serverAvailable) return;
    const attachment = serverRoot.attachment_id;
    const epoch = generation;
    const id = `server:${attachment}:${path}`;
    const existing = tabsRef.current.find((tab) => tab.id === id);
    if (existing) return show(existing);
    try {
      const file = await readServerText(client, attachment, path, () => serverAlive.current && epoch === serverGeneration.current && attachment === serverAttachment.current);
      if (epoch !== serverGeneration.current || attachment !== serverAttachment.current) return;
      // Another click may have finished the same read while this one waited.
      if (models.current.has(id)) return;
      models.current.set(id, monaco.editor.createModel(file.content, undefined, monaco.Uri.parse(`bob-server://${attachment}/${path}`)));
      addTab({ id, title: `${baseName(path)} (server)`, kind: 'server', path, dirty: false,
        note: `Read-only server file · ${serverRoot.label} · revision ${file.revision}. Saving, terminals and agent file context are unavailable for this tab.` });
    } catch (error) {
      if (serverAlive.current && epoch === serverGeneration.current && attachment === serverAttachment.current) setServerMessage(errorText(error));
    }
  }

  function renderServerTree(path: string, depth: number): ReactNode {
    const entries = serverChildren[path];
    if (!entries) return <p className="ide-loading">Reading…</p>;
    return <ul className="ide-tree">{entries.map((entry) => <li key={entry.path}>
      <button type="button" className={`ide-node${entry.dir ? ' dir' : ''}`} style={{ paddingLeft: 8 + depth * 12 }} onClick={() => {
        if (!entry.dir) { void openServerFile(entry.path); return; }
        setServerExpanded((prior) => { const next = new Set(prior); if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path); return next; });
        if (!serverChildren[entry.path]) void listServer(entry.path);
      }}><span className="twisty">{entry.dir ? serverExpanded.has(entry.path) ? '▾' : '▸' : ''}</span><span className="name">{entry.name}</span></button>
      {entry.dir && serverExpanded.has(entry.path) && renderServerTree(entry.path, depth + 1)}
    </li>)}</ul>;
  }

  function toggle(path: string) {
    setExpanded((set) => {
      const next = new Set(set);
      if (next.has(path)) next.delete(path);
      else {
        next.add(path);
        if (!children[path]) void listDir(path);
      }
      return next;
    });
  }

  function gitMark(path: string): string | null {
    if (!git?.repo || !git.top) return null;
    const rel = path.startsWith(`${git.top}/`) ? path.slice(git.top.length + 1) : null;
    if (!rel) return null;
    const code = git.changes[rel];
    if (code) return code.includes('?') ? 'U' : code.trim().slice(0, 1) || 'M';
    return null;
  }

  function renderTree(path: string, depth: number) {
    const entries = children[path];
    if (!entries) return <p className="ide-loading" style={{ paddingLeft: 12 + depth * 12 }}>…</p>;
    return (
      <ul className="ide-tree">
        {entries.map((entry) => {
          const mark = entry.dir ? null : gitMark(entry.path);
          return (
            <li key={entry.path}>
              <button
                type="button"
                className={`ide-node${entry.dir ? ' dir' : ''}${tabs.some((tab) => tab.id === `file:${entry.path}` && tab.id === activeTab) ? ' active' : ''}${mark ? ` git-${mark}` : ''}`}
                style={{ paddingLeft: 8 + depth * 12 }}
                onClick={() => (entry.dir ? toggle(entry.path) : void openFile(entry.path))}
                onDoubleClick={() => !entry.dir && mark && void openHeadDiff(entry.path)}
                title={mark ? `${entry.path} · ${mark === 'U' ? 'untracked' : 'changed'} — double-click to compare with HEAD` : entry.path}
              >
                <span className="twisty">{entry.dir ? (expanded.has(entry.path) ? '▾' : '▸') : ''}</span>
                <span className="name">{entry.name}</span>
                {mark && <span className="mark">{mark}</span>}
              </button>
              {entry.dir && expanded.has(entry.path) && renderTree(entry.path, depth + 1)}
            </li>
          );
        })}
      </ul>
    );
  }

  const current = tabs.find((tab) => tab.id === activeTab) ?? null;
  const currentPath = current?.kind === 'server' ? null : current?.path ?? null;
  useEffect(() => {
    onContext?.(currentPath);
  }, [currentPath, onContext]);
  const changed = git?.repo ? Object.keys(git.changes).length : 0;

  return (
    <div className={`ide${termOpen ? ' with-term' : ''}`}>
      <aside className="ide-explorer" aria-label="Explorer">
        <header>
          <h2>Explorer</h2>
          <button type="button" className="icon-button" onClick={() => void pickFolder()} title="Open a folder on this PC">
            Open…
          </button>
        </header>
        <label className="ide-pad">Source <select aria-label="File source" value={source} onChange={(event) => setSource(event.target.value as 'pc' | 'server')}>
          <option value="pc">This PC</option><option value="server">Server (read-only)</option>
        </select></label>
        {source === 'server' ? <div className="ide-start">
          {serverMessage && <p role="status">{serverMessage}</p>}
          {serverRoots.length > 0 && <label>Server project <select aria-label="Server project" value={serverRoot?.attachment_id ?? ''} onChange={(event) => chooseServerRoot(event.target.value)}>
            <option value="">Choose a project…</option>{serverRoots.map((item) => <option key={item.attachment_id} value={item.attachment_id}>{item.label}</option>)}
          </select></label>}
          {serverRoot && <><button type="button" className="ide-root" onClick={() => void listServer('')}>{serverRoot.label} · read-only ↻</button>{renderServerTree('', 0)}</>}
        </div> : root ? (
          <>
            <button type="button" className="ide-root" onClick={() => void listDir(root).then(() => refreshGit(root))} title={`${root} — click to refresh`}>
              {baseName(root)}
              {git?.repo && <small> · {git.branch || 'detached'}</small>}
            </button>
            <div className="ide-tree-scroll">
              {renderTree(root, 0)}
            </div>
          </>
        ) : (
          <div className="ide-start">
            <p className="subtle">Open a folder on this PC to browse, edit and run it.</p>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void openFolder(typedPath);
              }}
            >
              <label htmlFor="ide-path" className="sr-only">
                Folder path
              </label>
              <input id="ide-path" value={typedPath} onChange={(e) => setTypedPath(e.target.value)} placeholder="~/Projects/…" />
            </form>
            {recent.length > 0 && (
              <>
                <h2>Recent</h2>
                <ul className="ide-recent">
                  {recent.map((item) => (
                    <li key={item}>
                      <button type="button" className="link" onClick={() => void openFolder(item)} title={item}>
                        {baseName(item)} <small>{item}</small>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </aside>

      <section className="ide-center">
        <div className="ide-tabs" role="tablist" aria-label="Open editors">
          {tabs.map((tab) => (
            <div key={tab.id} className={`ide-tab${tab.id === activeTab ? ' active' : ''}`}>
              <button type="button" role="tab" aria-selected={tab.id === activeTab} onClick={() => show(tab)} title={tab.path ?? tab.title}>
                {tab.title}
                {tab.dirty && <span className="dirty"> ●</span>}
              </button>
              <button
                type="button"
                className={`close${closeArmed === tab.id ? ' armed' : ''}`}
                aria-label={`Close ${tab.title}`}
                onClick={() => closeTab(tab.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        {current?.note && <p className="ide-note">{current.note}</p>}
        <div className="ide-editors">
          <div ref={editorHost} className="ide-editor" hidden={current?.kind === 'head'} />
          <div ref={diffHost} className="ide-editor" hidden={current?.kind !== 'head'} />
          {!current && (
            <div className="ide-welcome">
              <h1>Workspace</h1>
              <p>Open a folder, pick a file, and talk to your agent on the right — it can see the open file.</p>
              <p className="subtle">Ctrl+S saves · double-click a changed file (M/U) to compare it with HEAD · proposals from agents open here as diffs.</p>
            </div>
          )}
        </div>

        <div className="ide-panel" hidden={!termOpen}>
          <div className="ide-panel-tabs">
            <button type="button" className={`panel-switch${panelTab === 'term' ? ' on' : ''}`} onClick={() => setPanelTab('term')}>
              Terminal
            </button>
            <button type="button" className={`panel-switch${panelTab === 'agents' ? ' on' : ''}`} onClick={() => setPanelTab('agents')}>
              Agents
            </button>
            <span className="sep" />
            {panelTab === 'term' && terms.map((item) => (
              <div key={item.id} className={`ide-tab${item.id === activeTerm ? ' active' : ''}`}>
                <button type="button" onClick={() => setActiveTerm(item.id)}>
                  {item.title}
                </button>
                <button type="button" className="close" aria-label={`Close ${item.title}`} onClick={() => closeTerminal(item.id)}>
                  ×
                </button>
              </div>
            ))}
            {panelTab === 'term' && (
              <button type="button" className="icon-button" onClick={() => void newTerminal()} disabled={!root || source === 'server' || current?.kind === 'server'} title="New terminal in the open PC folder">
                +
              </button>
            )}
            <button type="button" className="icon-button hide" onClick={() => setTermOpen(false)} title="Hide the terminal panel">
              ⌄
            </button>
          </div>
          <div className="ide-jobs" hidden={panelTab !== 'agents'}>
            {agents}
            <details className="server-jobs">
              <summary>Server jobs (proposals with diffs)</summary>
              {jobs}
            </details>
          </div>
          <div className="ide-terms" hidden={panelTab !== 'term'}>
            {terms.map((item) => (
              <TerminalView key={item.id} client={client} id={item.id} active={panelTab === 'term' && item.id === activeTerm} />
            ))}
            {!terms.length && <p className="subtle ide-pad">{root ? 'No terminal open. Press + to start one.' : 'Open a folder to start a terminal in it.'}</p>}
          </div>
        </div>

        <footer className="ide-status">
          <span>{source === 'server' ? `Server · ${serverRoot?.label ?? 'no project selected'} · read-only` : root ?? 'No folder'}</span>
          {git?.repo && (
            <span>
              ⎇ {git.branch || 'detached'} · {changed} change{changed === 1 ? '' : 's'}
            </span>
          )}
          {message && <span className="msg-line">{message}</span>}
          <span className="right">
            {cursor}
            {!termOpen && (
              <button type="button" className="link" onClick={() => setTermOpen(true)}>
                Terminal
              </button>
            )}
          </span>
        </footer>
      </section>
    </div>
  );
});

export default IdeEditor;
