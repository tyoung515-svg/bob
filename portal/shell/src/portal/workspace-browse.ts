import type { PortalClient } from './portal-client';
import type { WorkspaceBrowse } from './portal-protocol';

const segment = (s: unknown): s is string => typeof s === 'string' && !!s && !['.', '..'].includes(s) && !/[\/\\\x00-\x1f\x7f]/.test(s);
export function validWorkspaceBrowse(value: WorkspaceBrowse, project: string, path: string) {
  return !!value && value.project_id === project && value.path === path && typeof value.root_label === 'string' &&
    typeof value.truncated === 'boolean' && Array.isArray(value.entries) && value.entries.length <= 1000 &&
    value.entries.every(e => e && segment(e.name) &&
      ['dir', 'file', 'symlink'].includes(e.kind) && Number.isSafeInteger(e.size) && e.size >= 0 && Number.isFinite(e.mtime)) &&
    new Set(value.entries.map(e => e.name)).size === value.entries.length;
}

export async function readWorkspaceBrowse(client: PortalClient, project: string, path = '') {
  const value = await client.workspaceBrowse(project, path);
  if (!validWorkspaceBrowse(value, project, path)) throw new Error('Invalid workspace listing.');
  return value;
}
