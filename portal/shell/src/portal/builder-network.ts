import type { BuilderNetwork } from './portal-protocol';

// Canonical ASCII DNS hostnames only: no URL, port, wildcard, IP or local name.
export function builderHostname(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 253 && value.includes('.') && !/[^a-z0-9.-]/.test(value) &&
    value.split('.').every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) &&
    !/^[0-9]+$/.test(value.substring(value.lastIndexOf('.') + 1));
}
export function validBuilderNetwork(value: BuilderNetwork): boolean {
  return !!value && [value.allowlist, value.project_extra].every(list => Array.isArray(list) &&
    list.length <= 1000 && list.every(builderHostname) && new Set(list).size === list.length);
}
export function builderRevision(value: unknown): boolean {
  return (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ||
    typeof value === 'string' && value === value.trim() && /^(0|[1-9][0-9]*)$/.test(value)) && BigInt(value as string | number) <= 9223372036854775807n;
}
export const builderMethod = (value: unknown): value is string => typeof value === 'string' && value.length >= 1 && value.length <= 32 && !/[^A-Z]/.test(value);
