// Projects board (owner request 2026-11-02): server projects as Kanban cards.
// The server has no project status, so which column a card sits in (and its
// order) is kept on this PC in the "board" store.
import type { NativeInvoke } from '../workbench/workbench-client';
import type { Project } from './portal-protocol';

export type Column = 'backlog' | 'planned' | 'doing' | 'review' | 'done';

export const COLUMNS: { id: Column; label: string; hint: string }[] = [
  { id: 'backlog', label: 'Backlog', hint: 'Ideas and things not yet planned' },
  { id: 'planned', label: 'Planned', hint: 'Agreed with the PM, ready to pick up' },
  { id: 'doing', label: 'In progress', hint: 'Agent teams are working on it' },
  { id: 'review', label: 'Review', hint: 'Waiting for you to check' },
  { id: 'done', label: 'Done', hint: 'Finished' },
];

/** Card placement: column per project id, plus the order cards were placed in. */
export type Board = { columns: Record<string, Column>; order: string[] };

export const EMPTY_BOARD: Board = { columns: {}, order: [] };

export function columnOf(board: Board, id: string): Column {
  return board.columns[id] ?? 'backlog';
}

export function cardsIn(board: Board, projects: Project[], column: Column): Project[] {
  const rank = (id: string) => {
    const at = board.order.indexOf(id);
    return at < 0 ? Number.MAX_SAFE_INTEGER : at;
  };
  return projects
    .filter((project) => columnOf(board, project.id) === column)
    .sort((a, b) => rank(a.id) - rank(b.id) || String(b.updated_at).localeCompare(String(a.updated_at)));
}

/** Moves a card to the end of a column. */
export function moveCard(board: Board, id: string, column: Column): Board {
  return { columns: { ...board.columns, [id]: column }, order: [...board.order.filter((row) => row !== id), id] };
}

export function shiftColumn(column: Column, by: number): Column | null {
  const at = COLUMNS.findIndex((row) => row.id === column) + by;
  return at >= 0 && at < COLUMNS.length ? COLUMNS[at].id : null;
}

/** The board in plain text, so the PM agent can see it. */
export function boardSummary(board: Board, projects: Project[]): string {
  return COLUMNS.map((column) => {
    const cards = cardsIn(board, projects, column.id);
    const lines = cards.map((project) => `- ${project.name}${project.description ? `: ${project.description.split('\n')[0].slice(0, 140)}` : ''}`);
    return `## ${column.label}\n${lines.length ? lines.join('\n') : '- (empty)'}`;
  }).join('\n');
}

export function createBoardStore(invoke: NativeInvoke) {
  return {
    async load(): Promise<Board> {
      const parsed = JSON.parse(await invoke<string>('store_load', { name: 'board' })) as Partial<Board> | null;
      if (!parsed || typeof parsed !== 'object') return EMPTY_BOARD;
      return {
        columns: parsed.columns && typeof parsed.columns === 'object' ? (parsed.columns as Record<string, Column>) : {},
        order: Array.isArray(parsed.order) ? parsed.order.filter((id): id is string => typeof id === 'string') : [],
      };
    },
    save: (board: Board) => invoke<string>('store_save', { name: 'board', data: JSON.stringify(board, null, 2) }),
  };
}

export type BoardStore = ReturnType<typeof createBoardStore>;
