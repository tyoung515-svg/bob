import type { PermissionDisplay } from './portal-protocol';

// Callers pass only records fetched from the server, never model-authored cards.
export function permissionDisplay(value: unknown): PermissionDisplay | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const display = (value as { display?: Partial<PermissionDisplay> }).display;
  return display && typeof display.title === 'string' && typeof display.scope === 'string' &&
    Array.isArray(display.lines) && display.lines.every(line => typeof line === 'string')
    ? display as PermissionDisplay : null;
}

export function PermissionSummary({ value, fallback = 'Review the requested action.', collapseAction, fullRecord = false }: { value: unknown; fallback?: string; collapseAction?: 'Show command' | 'Show change'; fullRecord?: boolean }) {
  const display = permissionDisplay(value);
  const detail = value && typeof value === 'object' ? (value as { detail?: { command?: string; diff?: string } }).detail : null;
  const source = collapseAction && (collapseAction === 'Show change' ? detail?.diff : detail?.command);
  const action = <><p>{display?.title ?? fallback}</p>{display?.lines.map((line, i) => <p key={i}>{line}</p>)}{source && !display && <pre>{source}</pre>}</>;
  return <>
    {collapseAction ? <details><summary>{collapseAction}</summary>{action}</details> : action}
    {display && <p>{display.scope}</p>}
    {fullRecord ? <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(value, null, 2)}</pre> : <details><summary>Details</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(value, null, 2)}</pre></details>}
  </>;
}
