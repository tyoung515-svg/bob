import { ReadFaultText } from './provider-restart';
// Library (owner request 2026-11-02): a Steam/Xbox-style home for the apps and
// workflows Bob builds — browse, launch, open in the Workspace, and hand to an
// agent to drive or manage. "+ New app" plans one and starts a server project.
// The Workflow builder sketches what you want for the agent; it is not wired.
import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import { toPortalError } from './portal-protocol';
import type { PortalError, Project } from './portal-protocol';
import { KIND_LABEL, STATUS_LABEL, newApp, newSketch } from './library';
import type { AppKind, AppStatus, LibraryApp, LibraryStore, Sketch } from './library';
import { moveCard } from './board';
import type { BoardStore } from './board';
import { WorkflowSketch } from './WorkflowSketch';
import { WorkflowLibrary } from './WorkflowLibrary';
import { ago, hue, initials } from './agents';
import { LibraryResult } from './SaveToLibrary';

export type AppAsk = 'drive' | 'manage' | 'fix';

function Cover({ app, big = false }: { app: Pick<LibraryApp, 'id' | 'name' | 'kind'>; big?: boolean }) {
  const h = hue(app.name || app.id);
  return (
    <div
      className={`lib-cover ${app.kind}${big ? ' big' : ''}`}
      style={{ background: `linear-gradient(135deg, hsl(${h} 45% 30%), hsl(${(h + 50) % 360} 40% 16%))` }}
      aria-hidden="true"
    >
      <span>{initials(app.name || '?')}</span>
    </div>
  );
}

type Form = { mode: 'new' | 'add' | 'edit'; app: LibraryApp; idea: string };

export function LibraryView({
  client,
  store,
  boardStore,
  onPlan,
  onAsk,
  onSendSketch,
  onOpenFolder,
  onOpenProjects,
}: {
  client: PortalClient;
  store: LibraryStore;
  boardStore: BoardStore;
  /** A new app's first PM session, inside its new server project. */
  onPlan: (app: LibraryApp, idea: string) => void;
  onAsk: (app: LibraryApp, mode: AppAsk) => void;
  onSendSketch: (sketch: Sketch, app: LibraryApp | null) => void;
  onOpenFolder: (folder: string) => void;
  onOpenProjects: () => void;
}) {
  const [tab, setTab] = useState<'library' | 'workflows' | 'builder'>('library');
  const [apps, setApps] = useState<LibraryApp[]>([]);
  const [sketches, setSketches] = useState<Sketch[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [sketchId, setSketchId] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<AppKind | 'all'>('all');
  const [search, setSearch] = useState('');
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<PortalError | null>(null);
  const [message, setMessage] = useState('');
  const saveTimer = useRef<number | null>(null);

  useEffect(() => {
    void store.apps().then(setApps).catch((e: unknown) => setMessage(`The library could not be read: ${String(e)}`));
    void store
      .sketches()
      .then((rows) => {
        setSketches(rows);
        setSketchId((current) => current ?? rows[0]?.id ?? null);
      })
      .catch(() => undefined);
    void client
      .projects()
      .then((page) => setProjects(page.items))
      .catch(() => undefined);
  }, [store, client]);

  async function saveApps(next: LibraryApp[], note?: string) {
    setApps(next);
    try {
      await store.saveApps(next);
      if (note) setMessage(note);
    } catch (e) {
      setMessage(`Not saved: ${String(e)}`);
    }
  }

  function changeSketch(next: Sketch) {
    setSketches((rows) => {
      const all = rows.some((row) => row.id === next.id) ? rows.map((row) => (row.id === next.id ? next : row)) : [...rows, next];
      // Dragging changes a sketch many times a second; write once it settles.
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => void store.saveSketches(all).catch(() => setMessage('The sketch could not be saved.')), 600);
      return all;
    });
  }

  function addSketch(app: LibraryApp | null) {
    const sketch = newSketch(app ? `${app.name} workflow` : `Sketch ${sketches.length + 1}`);
    changeSketch(sketch);
    setSketchId(sketch.id);
    setTab('builder');
    if (app) void saveApps(apps.map((row) => (row.id === app.id ? { ...row, sketch_id: sketch.id } : row)));
  }

  async function launch(app: LibraryApp) {
    try {
      const pid = await store.launch(app.command, app.folder);
      await saveApps(
        apps.map((row) => (row.id === app.id ? { ...row, launches: row.launches + 1, last_launched: new Date().toISOString() } : row)),
        `Started ${app.name} (process ${pid}).`,
      );
    } catch (e) {
      setMessage(`${app.name} did not start: ${String(e)}`);
    }
  }

  async function submit() {
    if (!form || !form.app.name.trim()) return;
    setError(null);
    const app = { ...form.app, name: form.app.name.trim() };
    if (form.mode === 'new') {
      try {
        const created = await client.projectSave({ name: app.name, description: form.idea.trim(), instructions: '' });
        const project = created.project ?? null;
        const entry: LibraryApp = { ...app, status: 'planned', project_id: project?.id ?? null, summary: form.idea.trim() };
        await saveApps([entry, ...apps], `Planned ${entry.name}${project ? ' and started its project' : ''}.`);
        if (project) {
          try {
            await boardStore.save(moveCard(await boardStore.load(), project.id, 'planned'));
          } catch {
            // The project exists either way; its board column is a convenience.
          }
          setProjects((rows) => [project, ...rows]);
        }
        setSelected(entry.id);
        setForm(null);
        onPlan(entry, form.idea.trim());
      } catch (e) {
        setError(toPortalError(e));
      }
      return;
    }
    const exists = apps.some((row) => row.id === app.id);
    await saveApps(exists ? apps.map((row) => (row.id === app.id ? app : row)) : [app, ...apps], exists ? `Saved ${app.name}.` : `Added ${app.name}.`);
    setSelected(app.id);
    setForm(null);
  }

  async function remove(app: LibraryApp) {
    await saveApps(
      apps.filter((row) => row.id !== app.id),
      `Removed ${app.name} from the library (its project and files are untouched).`,
    );
    setSelected(null);
    setForm(null);
  }

  const needle = search.trim().toLowerCase();
  const shown = apps.filter(
    (app) => (kindFilter === 'all' || app.kind === kindFilter) && (!needle || `${app.name} ${app.summary}`.toLowerCase().includes(needle)),
  );
  const current = apps.find((app) => app.id === selected) ?? null;
  const recent = [...apps].filter((app) => app.last_launched).sort((a, b) => String(b.last_launched).localeCompare(String(a.last_launched))).slice(0, 6);
  const sketch = sketches.find((row) => row.id === sketchId) ?? null;
  const projectName = (id: string | null) => (id ? projects.find((row) => row.id === id)?.name ?? id : null);

  function formView(state: Form) {
    const set = (change: Partial<LibraryApp>) => setForm({ ...state, app: { ...state.app, ...change } });
    return (
      <form
        className="lib-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <h3>{state.mode === 'new' ? 'Plan a new app' : state.mode === 'add' ? 'Add an existing app' : `Edit ${state.app.name}`}</h3>
        <label htmlFor="lib-name">Name</label>
        <input id="lib-name" autoFocus value={state.app.name} onChange={(e) => set({ name: e.target.value })} />
        <label htmlFor="lib-kind">Kind</label>
        <select id="lib-kind" value={state.app.kind} onChange={(e) => set({ kind: e.target.value as AppKind })}>
          {(Object.keys(KIND_LABEL) as AppKind[]).map((kind) => (
            <option key={kind} value={kind}>
              {KIND_LABEL[kind]}
            </option>
          ))}
        </select>
        {state.mode === 'new' ? (
          <>
            <label htmlFor="lib-idea">What should it do? Rough is fine — the PM will ask.</label>
            <textarea id="lib-idea" rows={4} value={state.idea} onChange={(e) => setForm({ ...state, idea: e.target.value })} />
            <p className="subtle">Creates a project on the server, puts it in Planned on the board, and opens a PM session to plan it with you.</p>
          </>
        ) : (
          <>
            <label htmlFor="lib-summary">Summary</label>
            <textarea id="lib-summary" rows={2} value={state.app.summary} onChange={(e) => set({ summary: e.target.value })} />
            <label htmlFor="lib-command">Launch command on this PC (optional)</label>
            <input id="lib-command" placeholder="e.g. ./run.sh or xdg-open http://localhost:3000" value={state.app.command} onChange={(e) => set({ command: e.target.value })} />
            <label htmlFor="lib-folder">Folder on this PC (optional)</label>
            <input id="lib-folder" placeholder="~/Projects/…" value={state.app.folder} onChange={(e) => set({ folder: e.target.value })} />
            <label htmlFor="lib-status">Status</label>
            <select id="lib-status" value={state.app.status} onChange={(e) => set({ status: e.target.value as AppStatus })}>
              {(Object.keys(STATUS_LABEL) as AppStatus[]).map((status) => (
                <option key={status} value={status}>
                  {STATUS_LABEL[status]}
                </option>
              ))}
            </select>
            <label htmlFor="lib-project">Project</label>
            <select id="lib-project" value={state.app.project_id ?? ''} onChange={(e) => set({ project_id: e.target.value || null })}>
              <option value="">None</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </>
        )}
        {error && (
          <p role="alert" className="fault-inline">
            <ReadFaultText client={client} error={error} fallback={<><code>{error.code}</code> {error.detail}</>} />
          </p>
        )}
        <div className="row-actions">
          <button className="primary" disabled={!state.app.name.trim()}>
            {state.mode === 'new' ? 'Plan it with the PM' : 'Save'}
          </button>
          <button type="button" onClick={() => setForm(null)}>
            Cancel
          </button>
          {state.mode === 'edit' && (
            <button type="button" className="danger" onClick={() => void remove(state.app)}>
              Remove from library
            </button>
          )}
        </div>
      </form>
    );
  }

  function detail(app: LibraryApp) {
    const project = projectName(app.project_id);
    const attached = sketches.find((row) => row.id === app.sketch_id) ?? null;
    return (
      <div className="lib-detail">
        <div className="lib-hero" style={{ background: `linear-gradient(120deg, hsl(${hue(app.name)} 45% 26%), #121417 75%)` }}>
          <Cover app={app} big />
          <div className="lib-hero-text">
            <small>
              {KIND_LABEL[app.kind]} · {STATUS_LABEL[app.status]}
            </small>
            <h1>{app.name}</h1>
            <div className="lib-play">
              {app.project_id ? <LibraryResult client={client} projectId={app.project_id} /> : <><button type="button" className="primary play" disabled={!app.command} onClick={() => void launch(app)} title={app.command || 'Add a launch command in Edit'}>
                ▶ Launch
              </button>
              <button type="button" disabled={!app.folder} onClick={() => onOpenFolder(app.folder)}>
                Open in Workspace
              </button></>}
              <button type="button" onClick={() => setForm({ mode: 'edit', app, idea: '' })}>
                Edit
              </button>
            </div>
          </div>
        </div>
        <div className="lib-body">
          <section>
            <h4>About</h4>
            <p>{app.summary || <span className="subtle">No summary yet.</span>}</p>
            <h4>Agents</h4>
            <div className="work-on-it">
              <button type="button" onClick={() => onAsk(app, 'drive')} title="Use it or run something with it">
                Drive it
              </button>
              <button type="button" onClick={() => onAsk(app, 'manage')} title="Review, update, document">
                Manage it
              </button>
              <button type="button" onClick={() => onAsk(app, 'fix')}>
                Fix a problem
              </button>
            </div>
            <h4>Workflow sketch</h4>
            {attached ? (
              <p>
                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    setSketchId(attached.id);
                    setTab('builder');
                  }}
                >
                  {attached.name}
                </button>{' '}
                <small className="subtle">{attached.nodes.length} steps</small>
              </p>
            ) : (
              <button type="button" onClick={() => addSketch(app)}>
                Sketch its workflow
              </button>
            )}
          </section>
          <dl className="facts lib-stats">
            <dt>launches</dt>
            <dd>{app.launches}</dd>
            <dt>last launched</dt>
            <dd>{app.last_launched ? `${ago(app.last_launched)} ago` : 'never'}</dd>
            <dt>project</dt>
            <dd>
              {project ? (
                <button type="button" className="link" onClick={onOpenProjects}>
                  {project}
                </button>
              ) : (
                'none'
              )}
            </dd>
            <dt>folder</dt>
            <dd>{app.folder ? <code>{app.folder}</code> : '—'}</dd>
            <dt>command</dt>
            <dd>{app.command ? <code>{app.command}</code> : '—'}</dd>
            <dt>added</dt>
            <dd>{app.created_at.slice(0, 10)}</dd>
          </dl>
        </div>
      </div>
    );
  }

  return (
    <section className="lib" aria-label="Library">
      <header className="cal-toolbar">
        <h2 className="cal-title">Library</h2>
        <div className="seg" role="group" aria-label="Library section">
          <button type="button" aria-pressed={tab === 'library'} onClick={() => setTab('library')}>
            Apps
          </button>
          <button type="button" aria-pressed={tab === 'builder'} onClick={() => setTab('builder')}>
            Workflow builder
          </button>
          <button type="button" aria-pressed={tab === 'workflows'} onClick={() => setTab('workflows')}>Workflows</button>
        </div>
        <span className="spacer" />
        {tab === 'library' && (
          <>
            <input className="board-search" type="search" aria-label="Search the library" placeholder="Search…" value={search} onChange={(e) => setSearch(e.target.value)} />
            <button type="button" onClick={() => setForm({ mode: 'add', app: { ...newApp(''), status: 'ready' }, idea: '' })}>
              Add existing
            </button>
            <button type="button" className="primary" onClick={() => setForm({ mode: 'new', app: newApp(''), idea: '' })}>
              + New app
            </button>
          </>
        )}
        {tab === 'builder' && (
          <button type="button" className="primary" onClick={() => addSketch(null)}>
            + New sketch
          </button>
        )}
      </header>

      {tab === 'workflows' ? <WorkflowLibrary onSketch={async sketch => {
        if (saveTimer.current !== null) {window.clearTimeout(saveTimer.current);saveTimer.current=null;}
        const all=[...sketches,sketch];await store.saveSketches(all);setSketches(all);setSketchId(sketch.id);setTab('builder');
      }} /> : tab === 'library' ? (
        <div className="lib-main">
          <nav className="lib-list" aria-label="Library items">
            <div className="lib-filters">
              {(['all', 'app', 'workflow', 'tool'] as const).map((kind) => (
                <button key={kind} type="button" aria-pressed={kindFilter === kind} onClick={() => setKindFilter(kind)}>
                  {kind === 'all' ? 'All' : `${KIND_LABEL[kind]}s`}
                </button>
              ))}
            </div>
            <button type="button" className={`lib-home${selected === null ? ' active' : ''}`} onClick={() => setSelected(null)}>
              Home
            </button>
            {shown.map((app) => (
              <button key={app.id} type="button" className={`lib-item${selected === app.id ? ' active' : ''}`} onClick={() => setSelected(app.id)}>
                <Cover app={app} />
                <span>
                  <strong>{app.name}</strong>
                  <small>
                    {KIND_LABEL[app.kind]} · {STATUS_LABEL[app.status]}
                  </small>
                </span>
              </button>
            ))}
            {!apps.length && <p className="subtle ide-pad">Nothing here yet.</p>}
          </nav>
          <div className="lib-stage">
            {form ? (
              formView(form)
            ) : current ? (
              detail(current)
            ) : apps.length ? (
              <div className="lib-home-view">
                {recent.length > 0 && (
                  <>
                    <h3>Jump back in</h3>
                    <div className="lib-tiles recent">
                      {recent.map((app) => (
                        <button key={app.id} type="button" className="lib-tile" onClick={() => setSelected(app.id)}>
                          <Cover app={app} />
                          <strong>{app.name}</strong>
                          <small>{app.last_launched ? `${ago(app.last_launched)} ago` : ''}</small>
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <h3>All {kindFilter === 'all' ? 'items' : `${KIND_LABEL[kindFilter].toLowerCase()}s`} · {shown.length}</h3>
                <div className="lib-tiles">
                  {shown.map((app) => (
                    <button key={app.id} type="button" className="lib-tile" onClick={() => setSelected(app.id)}>
                      <Cover app={app} />
                      <strong>{app.name}</strong>
                      <small>
                        {KIND_LABEL[app.kind]} · {STATUS_LABEL[app.status]}
                      </small>
                    </button>
                  ))}
                  <button type="button" className="lib-tile add" onClick={() => setForm({ mode: 'new', app: newApp(''), idea: '' })}>
                    <div className="lib-cover plus">+</div>
                    <strong>New app</strong>
                    <small>Plan it with the PM</small>
                  </button>
                </div>
              </div>
            ) : (
              <div className="lib-empty">
                <h1>Your library is empty</h1>
                <p>Apps and workflows Bob builds for you live here, ready to launch and manage with your agents.</p>
                <div className="row-actions">
                  <button type="button" className="primary" onClick={() => setForm({ mode: 'new', app: newApp(''), idea: '' })}>
                    + Plan a new app
                  </button>
                  <button type="button" onClick={() => setForm({ mode: 'add', app: { ...newApp(''), status: 'ready' }, idea: '' })}>
                    Add an existing app
                  </button>
                </div>
                <p className="subtle">The server has no list of built apps yet, so the library holds what you add here (kept on this PC).</p>
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="lib-builder">
          <nav className="lib-list" aria-label="Sketches">
            {sketches.map((row) => (
              <div key={row.id} className={`lib-sketch-row${row.id === sketchId ? ' active' : ''}`}>
                <button type="button" onClick={() => setSketchId(row.id)}>
                  <strong>{row.name}</strong>
                  <small>
                    {row.nodes.length} steps · {ago(row.updated_at)}
                  </small>
                </button>
              </div>
            ))}
            {!sketches.length && <p className="subtle ide-pad">No sketches yet. Press + New sketch.</p>}
            {sketch && (
              <div className="lib-sketch-meta">
                <label htmlFor="sk-name">Name</label>
                <input id="sk-name" value={sketch.name} onChange={(e) => changeSketch({ ...sketch, name: e.target.value })} />
                <label htmlFor="sk-app">For app</label>
                <select
                  id="sk-app"
                  value={apps.find((app) => app.sketch_id === sketch.id)?.id ?? ''}
                  onChange={(e) =>
                    void saveApps(
                      apps.map((app) =>
                        app.id === e.target.value ? { ...app, sketch_id: sketch.id } : app.sketch_id === sketch.id ? { ...app, sketch_id: null } : app,
                      ),
                    )
                  }
                >
                  <option value="">None</option>
                  {apps.map((app) => (
                    <option key={app.id} value={app.id}>
                      {app.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="link danger-link"
                  onClick={() => {
                    const rest = sketches.filter((row) => row.id !== sketch.id);
                    setSketches(rest);
                    void store.saveSketches(rest);
                    setSketchId(rest[0]?.id ?? null);
                  }}
                >
                  Delete sketch
                </button>
              </div>
            )}
          </nav>
          {sketch ? (
            <WorkflowSketch
              sketch={sketch}
              onChange={changeSketch}
              onSend={(row) => onSendSketch(row, apps.find((app) => app.sketch_id === row.id) ?? null)}
            />
          ) : (
            <div className="lib-empty">
              <h1>Workflow builder</h1>
              <p>Sketch what you want when words are hard: drop in steps, link them, and send the drawing to an agent to build from.</p>
              <button type="button" className="primary" onClick={() => addSketch(null)}>
                + New sketch
              </button>
            </div>
          )}
        </div>
      )}
      <p className="cal-message">{message || (tab === 'library' ? 'Launch, open, and hand apps to your agents. Kept on this PC until the server has an app list.' : tab === 'workflows' ? 'Workflow definitions are saved on Bob. Review a proposal before starting work.' : 'Sketches are saved on this PC. They are drawings for the agent, not running workflows.')}</p>
    </section>
  );
}

