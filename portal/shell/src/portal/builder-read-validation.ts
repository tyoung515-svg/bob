import type { BuilderApproval, BuilderModeState } from './portal-protocol';
import { builderModes, builderTime } from './portal-protocol';
import { builderHostname, builderMethod, builderRevision, validBuilderNetwork } from './builder-network';
const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);

export function validBuilderApproval(value: BuilderApproval, conversationId: string, approvalId: string): boolean {
  return !!value && value.schema_version === 1 && value.conversation_id === conversationId && value.approval_id === approvalId &&
    ['write', 'edit', 'command', 'network', 'workspace'].includes(value.kind) && builderModes.includes(value.mode) &&
    ['pending', 'allowed', 'denied', 'expired', 'stopped'].includes(value.state) && typeof value.summary === 'string' &&
    (value.execution_state === undefined || ['pending','running','completed','failed','stopped','unknown'].includes(value.execution_state)) &&
    Number.isFinite(builderTime(value.expires_at)) && !!value.detail && typeof value.detail === 'object' && !Array.isArray(value.detail) &&
    ['path', 'diff', 'command', 'cwd'].every(k => !(k in value.detail) || typeof value.detail[k as 'path'] === 'string') &&
    (value.detail.network === undefined || typeof value.detail.network === 'boolean') &&
    (value.kind === 'workspace' ? ['create', 'mount'].includes(value.detail.action ?? '') &&
      typeof value.detail.root_label === 'string' && typeof value.detail.name === 'string' &&
      (value.detail.from_project_id === null || id(value.detail.from_project_id)) &&
      (value.detail.action !== 'mount' || id(value.detail.project_id)) :
      value.kind === 'network' ? builderHostname(value.detail.domain) &&
      (value.detail.method == null || builderMethod(value.detail.method)) :
      value.kind === 'command' ? !!value.detail.command : !!value.detail.path);
}

export function builderTerminal(record: BuilderApproval): boolean {
  return ['denied','expired','stopped'].includes(record.state) ||
    ['completed','failed','stopped','unknown'].includes(record.execution_state ?? '') ||
    record.result?.exit_code !== undefined || record.result?.status === 'completed';
}

export function validBuilderMode(value: BuilderModeState, conversationId: string): boolean {
  return !!value && value.conversation_id === conversationId && (value.project_id === null || typeof value.project_id === 'string') &&
    (value.result_path == null || typeof value.result_path === 'string' && !value.result_path.startsWith('/') && !value.result_path.includes('..') && !value.result_path.includes('\\')) &&
    builderModes.includes(value.mode) && validBuilderNetwork(value.network) && builderRevision(value.revision) &&
    (value.expires_at === null || Number.isFinite(builderTime(value.expires_at))) &&
    (value.mode !== 'yolo' || Number.isFinite(builderTime(value.expires_at)));
}

