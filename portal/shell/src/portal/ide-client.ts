import type { NativeInvoke } from '../workbench/workbench-client';
import type { ServerRoot, ServerEntry, ServerFileChunk } from './portal-protocol';

// PC commands stay local; owner server reads use only opaque registered roots.

export type IdeEntry = { name: string; path: string; dir: boolean; size: number };
export type IdeFile = { path: string; content: string; binary: boolean; too_large: boolean };
export type IdeGit = { repo: boolean; branch: string; changes: Record<string, string>; top: string };
export type TermFrame = { id: string; data: string | null; exit: boolean };

export const TERM_EVENT = 'ide://term';

export function createIdeClient(invoke: NativeInvoke) {
  return {
    serverRoots: () => invoke<{ items: ServerRoot[] }>('portal_files_roots'),
    serverList: (attachment_id: string, path = '', cursor?: string) =>
      invoke<{ items: ServerEntry[]; next_cursor: string | null }>('portal_files_list', { attachment_id, path, ...(cursor ? { cursor } : {}) }),
    serverRead: (attachment_id: string, path: string, offset = 0, max_bytes = 65536) =>
      invoke<ServerFileChunk>('portal_files_read', { attachment_id, path, offset, max_bytes }),
    pickFolder: () => invoke<string | null>('ide_pick_folder'),
    listDir: (path: string) => invoke<IdeEntry[]>('ide_list_dir', { path }),
    readFile: (path: string) => invoke<IdeFile>('ide_read_file', { path }),
    writeFile: (path: string, content: string) => invoke<null>('ide_write_file', { path, content }),
    gitStatus: (root: string) => invoke<IdeGit>('ide_git_status', { root }),
    gitHead: (path: string) => invoke<string | null>('ide_git_head', { path }),
    termOpen: (cwd: string, cols: number, rows: number) => invoke<string>('ide_term_open', { cwd, cols, rows }),
    termWrite: (id: string, data: string) => invoke<null>('ide_term_write', { id, data }),
    termResize: (id: string, cols: number, rows: number) => invoke<null>('ide_term_resize', { id, cols, rows }),
    termClose: (id: string) => invoke<null>('ide_term_close', { id }),
  };
}

export type IdeClient = ReturnType<typeof createIdeClient>;

/** Join bytes before decoding: UTF-8 codepoints may straddle base64 chunks. */
export async function readServerText(client: IdeClient, attachment: string, path: string, isCurrent = () => true) {
  const chunks: Uint8Array[] = [];
  let offset = 0;
  let revision: string | undefined;
  const cap = 4 * 1024 * 1024;
  for (;;) {
    if (!isCurrent()) throw new Error('Connection changed');
    const page = await client.serverRead(attachment, path, offset, 65536);
    if (!isCurrent()) throw new Error('Connection changed');
    if (revision !== undefined && revision !== page.revision) throw new Error('Server file changed during reading. Reopen it to retry.');
    revision = page.revision;
    const bytes = page.encoding === 'base64'
      ? Uint8Array.from(atob(page.content), (char) => char.charCodeAt(0))
      : new TextEncoder().encode(page.content);
    if (offset + bytes.length > cap) throw new Error('Server file is larger than the 4 MB editor limit.');
    chunks.push(bytes);
    const next = offset + bytes.length;
    if (!page.truncated) {
      const joined = new Uint8Array(next);
      let at = 0;
      for (const chunk of chunks) { joined.set(chunk, at); at += chunk.length; }
      if (joined.includes(0)) throw new Error('Binary server file — not opened in the editor.');
      try { return { content: new TextDecoder('utf-8', { fatal: true }).decode(joined), revision }; }
      catch { throw new Error('Binary server file — not opened in the editor.'); }
    }
    if (next <= offset || page.next_offset !== next) throw new Error('Invalid server file offset.');
    offset = next;
  }
}

export function errorText(error: unknown): string {
  return typeof error === 'string' ? error : error instanceof Error ? error.message : String(error);
}
