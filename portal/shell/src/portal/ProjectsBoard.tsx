import { ReadFaultText } from './provider-restart';
// Projects as a Kanban board (owner request 2026-11-02). Agent teams are the
// workers; a PM agent works this screen with the owner in the right column.
// Cards are the server's projects; their columns are kept on this PC.
import { useCallback, useEffect, useState } from 'react';
import type { PortalClient } from './portal-client';
import { toPortalError, txt } from './portal-protocol';
import type { Catalog, Conversation, Identity, PortalError, Project } from './portal-protocol';
import { ownerEligible } from './durable-runs';
import { ProjectBudget } from './ProjectBudget';
import { BuilderNetworkSettings } from './BuilderNetworkSettings';
import { COLUMNS, EMPTY_BOARD, boardSummary, cardsIn, columnOf, moveCard, shiftColumn } from './board';
import type { Board, BoardStore, Column } from './board';
import { ago } from './agents';

type SortKey = 'name' | 'status' | 'sessions' | 'updated';

const COLUMN_RANK: Record<Column, number> = { backlog: 0, planned: 1, doing: 2, review: 3, done: 4 };

export function ProjectsBoard({
  client,
  store,
  conversations,
  openConversationId,
  onPickUp,
  onAskPm,
  onOpenSession,
  onAssigned,
  catalog, identity, scope = '',
}: {
  client: PortalClient;
  catalog?: Catalog | null;
  identity?: Identity | null;
  scope?: string;
  store: BoardStore;
  conversations: Conversation[];
  openConversationId: string | null;
  /** Start a PM session inside the project. */
  onPickUp: (project: Project) => void;
  /** Talk to the PM about the whole board. */
  onAskPm: (summary: string) => void;
  onOpenSession: (id: string) => void;
  onAssigned: () => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [board, setBoard] = useState<Board>(EMPTY_BOARD);
  const [error, setError] = useState<PortalError | null>(null);
  const [message, setMessage] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<Column | null>(null);
  const [search, setSearch] = useState('');
  const [newName, setNewName] = useState('');
  const [edit, setEdit] = useState<{ name: string; description: string; instructions: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // Board or a plain list of the same server projects; nothing is added to the list that the server did not return.
  const [layout, setLayout] = useState<'board' | 'list'>('board');
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: 'updated', desc: true });

  const load = useCallback(async () => {
    setError(null);
    try {
      setProjects((await client.projects()).items);
    } catch (e) {
      setError(toPortalError(e));
    }
  }, [client]);

  useEffect(() => {
    void load();
    store
      .load()
      .then(setBoard)
      .catch((e: unknown) => setMessage(`The board layout could not be read: ${String(e)}`));
  }, [load, store]);

  async function place(id: string, column: Column) {
    const next = moveCard(board, id, column);
    setBoard(next);
    try {
      await store.save(next);
    } catch (e) {
      setMessage(`Not saved: ${String(e)}`);
    }
  }

  async function create() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    try {
      const result = await client.projectSave({ name, description: '', instructions: '' });
      setNewName('');
      await load();
      if (result.project) {
        await place(result.project.id, 'backlog');
        setSelected(result.project.id);
      }
      setMessage(`Created “${name}” on the server.`);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit(project: Project) {
    if (!edit) return;
    setBusy(true);
    try {
      await client.projectSave({ id: project.id, name: edit.name, description: edit.description, instructions: edit.instructions });
      setEdit(null);
      await load();
      setMessage(`Saved “${edit.name}” on the server.`);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function assignOpen(project: Project) {
    if (!openConversationId) return;
    setBusy(true);
    try {
      await client.conversationAssignProject(openConversationId, project.id);
      onAssigned();
      await load();
      setMessage(`The open chat session now belongs to “${project.name}”.`);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  function pickUp(project: Project) {
    const column = columnOf(board, project.id);
    if (column === 'backlog' || column === 'planned') void place(project.id, 'doing');
    onPickUp(project);
  }

  const needle = search.trim().toLowerCase();
  const shown = projects.filter((project) => !needle || `${project.name} ${project.description}`.toLowerCase().includes(needle));
  const current = projects.find((project) => project.id === selected) ?? null;
  const sessions = current ? conversations.filter((row) => row.project_id === current.id) : [];

  return (
    <section className="board" aria-label="Projects board">
      <header className="cal-toolbar">
        <h2 className="cal-title">Projects</h2>
        <small className="subtle">{projects.length} on the server</small>
        <div className="seg" role="group" aria-label="Projects layout">
          <button type="button" aria-pressed={layout === 'board'} onClick={() => setLayout('board')}>
            Board
          </button>
          <button type="button" aria-pressed={layout === 'list'} onClick={() => setLayout('list')}>
            List
          </button>
        </div>
        <span className="spacer" />
        <input
          className="board-search"
          type="search"
          aria-label="Search projects"
          placeholder="Search projects…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <form
          className="board-new"
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <input aria-label="New project name" placeholder="New project name" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <button disabled={busy || !newName.trim()}>Add</button>
        </form>
        <button type="button" className="primary" onClick={() => onAskPm(boardSummary(board, projects))}>
          Ask the PM…
        </button>
      </header>
      {error && (
        <p role="alert" className="fault-inline">
          <ReadFaultText client={client} error={error} fallback={<><code>{error.code}</code> {error.detail}</>} />
        </p>
      )}

      {layout === 'list' && (
        <div className="project-list">
          {shown.length ? (
            <table>
              <thead>
                <tr>
                  {(
                    [
                      ['name', 'Project'],
                      ['status', 'Status'],
                      ['sessions', 'Sessions'],
                      ['updated', 'Updated'],
                    ] as [SortKey, string][]
                  ).map(([key, label]) => (
                    <th key={key} aria-sort={sort.key === key ? (sort.desc ? 'descending' : 'ascending') : 'none'}>
                      <button type="button" onClick={() => setSort({ key, desc: sort.key === key ? !sort.desc : key !== 'name' })}>
                        {label}
                        {sort.key === key ? (sort.desc ? ' ▾' : ' ▴') : ''}
                      </button>
                    </th>
                  ))}
                  <th>Summary</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {[...shown]
                  .sort((a, b) => {
                    const by =
                      sort.key === 'name'
                        ? a.name.localeCompare(b.name)
                        : sort.key === 'status'
                          ? COLUMN_RANK[columnOf(board, a.id)] - COLUMN_RANK[columnOf(board, b.id)]
                          : sort.key === 'sessions'
                            ? a.conversation_count - b.conversation_count
                            : String(a.updated_at).localeCompare(String(b.updated_at));
                    return sort.desc ? -by : by;
                  })
                  .map((project) => (
                    <tr key={project.id} className={selected === project.id ? 'selected' : undefined} onClick={() => setSelected(project.id)}>
                      <td>
                        <strong>{txt(project.name, 'Untitled project')}</strong>
                      </td>
                      <td>
                        <select
                          aria-label={`Status of ${project.name}`}
                          value={columnOf(board, project.id)}
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) => void place(project.id, event.target.value as Column)}
                        >
                          {COLUMNS.map((column) => (
                            <option key={column.id} value={column.id}>
                              {column.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>{project.conversation_count}</td>
                      <td>{project.updated_at ? ago(project.updated_at) : '—'}</td>
                      <td className="summary">{project.description || <span className="subtle">—</span>}</td>
                      <td>
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            pickUp(project);
                          }}
                        >
                          Pick up
                        </button>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : (
            <p className="board-empty list-empty">
              {projects.length
                ? 'No project matches the search.'
                : 'No projects on the server yet. Add one above, or ask the PM to help you shape one.'}
            </p>
          )}
        </div>
      )}

      <div className="board-columns" hidden={layout !== 'board'}>
        {COLUMNS.map((column) => {
          const cards = cardsIn(board, shown, column.id);
          return (
            <div
              key={column.id}
              className={`board-column ${column.id}${over === column.id ? ' over' : ''}`}
              onDragOver={(event) => {
                event.preventDefault();
                setOver(column.id);
              }}
              onDragLeave={() => setOver((now) => (now === column.id ? null : now))}
              onDrop={(event) => {
                event.preventDefault();
                const id = event.dataTransfer.getData('text/plain') || dragging;
                setOver(null);
                setDragging(null);
                if (id) void place(id, column.id);
              }}
            >
              <header title={column.hint}>
                <h3>{column.label}</h3>
                <small>{cards.length}</small>
              </header>
              <div className="board-cards">
                {cards.map((project) => (
                  <article
                    key={project.id}
                    className={`board-card${selected === project.id ? ' selected' : ''}${dragging === project.id ? ' dragging' : ''}`}
                    draggable
                    onDragStart={(event) => {
                      event.dataTransfer.setData('text/plain', project.id);
                      setDragging(project.id);
                    }}
                    onDragEnd={() => setDragging(null)}
                  >
                    <button type="button" className="card-main" onClick={() => setSelected(selected === project.id ? null : project.id)}>
                      <strong>{txt(project.name, 'Untitled project')}</strong>
                      {project.description && <span className="card-desc">{project.description}</span>}
                      <small>
                        {project.conversation_count} session{project.conversation_count === 1 ? '' : 's'}
                        {project.updated_at ? ` · ${ago(project.updated_at)}` : ''}
                        {project.default_face_id ? ` · ${project.default_face_id}` : ''}
                      </small>
                    </button>
                    <div className="card-actions">
                      <button
                        type="button"
                        aria-label="Move left"
                        disabled={!shiftColumn(column.id, -1)}
                        onClick={() => {
                          const to = shiftColumn(column.id, -1);
                          if (to) void place(project.id, to);
                        }}
                      >
                        ◂
                      </button>
                      <button type="button" className="pick" onClick={() => pickUp(project)} title="Open a PM session for this project">
                        Pick up
                      </button>
                      <button
                        type="button"
                        aria-label="Move right"
                        disabled={!shiftColumn(column.id, 1)}
                        onClick={() => {
                          const to = shiftColumn(column.id, 1);
                          if (to) void place(project.id, to);
                        }}
                      >
                        ▸
                      </button>
                    </div>
                  </article>
                ))}
                {!cards.length && <p className="board-empty">{column.hint}</p>}
              </div>
            </div>
          );
        })}
      </div>

      {current && (
        <section className="board-detail" aria-label="Project detail">
          <header>
            <h3>{txt(current.name, 'Untitled project')}</h3>
            <small className="subtle">
              {COLUMNS.find((row) => row.id === columnOf(board, current.id))?.label} · <code>{current.id}</code>
            </small>
            <span className="spacer" />
            <button type="button" className="primary" onClick={() => pickUp(current)}>
              Pick up with the PM
            </button>
            {!edit && (
              <button type="button" onClick={() => setEdit({ name: current.name, description: current.description, instructions: current.instructions })}>
                Edit
              </button>
            )}
            <button type="button" onClick={() => void assignOpen(current)} disabled={busy || !openConversationId} title="Moves the chat session open on the right into this project">
              Add open chat to project
            </button>
            <button type="button" className="link" onClick={() => setSelected(null)}>
              Close
            </button>
          </header>
          <ProjectBudget key={`${scope}:${current.id}`} client={client} catalog={catalog} scope={scope} projectId={current.id} owner={ownerEligible(identity, catalog)} />
          <BuilderNetworkSettings key={`network:${scope}:${current.id}`} client={client} catalog={catalog} scope={scope} projectId={current.id} conversationId={sessions[0]?.id} owner={ownerEligible(identity, catalog)} />
          {edit ? (
            <form
              className="board-edit"
              onSubmit={(event) => {
                event.preventDefault();
                void saveEdit(current);
              }}
            >
              <label htmlFor="board-name">Name</label>
              <input id="board-name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
              <label htmlFor="board-desc">Summary</label>
              <textarea id="board-desc" rows={3} value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} />
              <label htmlFor="board-instr">Instructions for the agents</label>
              <textarea id="board-instr" rows={4} value={edit.instructions} onChange={(e) => setEdit({ ...edit, instructions: e.target.value })} />
              <div className="row-actions">
                <button className="primary" disabled={busy || !edit.name.trim()}>
                  Save to server
                </button>
                <button type="button" onClick={() => setEdit(null)}>
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <div className="board-detail-body">
              <div>
                <h4>Summary</h4>
                <p>{current.description || <span className="subtle">No summary yet — ask the PM to write one.</span>}</p>
                <h4>Instructions for the agents</h4>
                <p className="pre">{current.instructions || <span className="subtle">None.</span>}</p>
              </div>
              <div>
                <h4>Sessions · {sessions.length}</h4>
                {sessions.length ? (
                  <ul className="board-sessions">
                    {sessions.map((row) => (
                      <li key={row.id}>
                        <button type="button" className="link" onClick={() => onOpenSession(row.id)}>
                          {txt(row.title, 'Untitled session')}
                        </button>{' '}
                        <small className="subtle">
                          {row.face_id ?? 'Bob'} · {ago(String(row.updated_at ?? row.created_at ?? ''))}
                        </small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="subtle">No sessions yet. Pick it up to start one with the PM.</p>
                )}
              </div>
            </div>
          )}
        </section>
      )}
      <p className="cal-message">
        {message ||
          'Drag cards between columns or use ◂ ▸. Pick up a project to work it with the PM on the right; agent teams do the work. Columns are saved on this PC; projects live on the server.'}
      </p>
    </section>
  );
}
