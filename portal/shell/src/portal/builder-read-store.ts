import { useEffect, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { BuilderApproval, BuilderModeState, PortalError } from './portal-protocol';
import { builderTime, isRateLimited, toPortalError } from './portal-protocol';
import { builderTerminal, validBuilderApproval, validBuilderMode } from './builder-read-validation';

// Stores are scoped to a native connection, owner scope and conversation. No
// timer survives the last subscriber; cached terminal records survive navigation.
const stores = new WeakMap<PortalClient, Map<string, BuilderReadStore>>();
const retryDeadlines = new WeakMap<PortalClient, number>();
export class BuilderReadStore {
  mode: BuilderModeState | null = null;
  records = new Map<string, BuilderApproval>();
  recordErrors = new Map<string, string>();
  error = '';
  fault: PortalError | null = null;
  private listeners = new Set<() => void>();
  private ids = new Map<string, number>();
  private timer: ReturnType<typeof setTimeout> | undefined;
  private flight: Promise<void> | null = null;
  private next = 0;
  private failures = 0;
  private versions = new Map<string, number>();
  private live = false;
  constructor(private client: PortalClient, private conversation: string) {}
  private emit = () => this.listeners.forEach(f => f());
  private wake = () => {
    if (!this.error && this.mode?.mode === 'yolo' && builderTime(this.mode.expires_at) <= Date.now()) this.next = 0;
    if (document.visibilityState !== 'hidden') this.schedule(0);
  };
  private changed = (event: Event) => {
    const id = (event as CustomEvent<{ conversation_id?: string }>).detail?.conversation_id;
    if (id && id !== this.conversation) return;
    this.next = 0; this.wake();
  };
  setLive = (live: boolean) => {
    if (this.live === live) return;
    this.live = live;
    if (live) this.schedule(30000);
    else if (!this.waiting()) clearTimeout(this.timer);
  };
  private waiting = () => [...this.ids.keys()].some(id => !this.recordErrors.has(id) &&
    (!this.records.has(id) || !builderTerminal(this.records.get(id)!))) ||
    [...this.records.values()].some(r => r.state === 'pending' && builderTime(r.expires_at) > Date.now());
  subscribe(listener: () => void, id?: string) {
    this.listeners.add(listener);
    if (id) this.ids.set(id, (this.ids.get(id) ?? 0) + 1);
    if (this.listeners.size === 1) {
      document.addEventListener('visibilitychange', this.wake);
      window.addEventListener('online', this.wake);
      window.addEventListener('focus', this.wake);
      window.addEventListener('bob-builder-changed', this.changed);
    }
    if (!this.error && (this.listeners.size === 1 || !this.mode || id && !this.records.has(id))) this.next = 0;
    // Effects register all cards before this microtask starts the shared read.
    queueMicrotask(() => { if (this.listeners.size) void this.read(); });
    return () => {
      this.listeners.delete(listener);
      if (id) { const n = this.ids.get(id)! - 1; if (n) this.ids.set(id, n); else this.ids.delete(id); }
      if (!this.listeners.size) {
        clearTimeout(this.timer);
        document.removeEventListener('visibilitychange', this.wake);
        window.removeEventListener('online', this.wake);
        window.removeEventListener('focus', this.wake);
        window.removeEventListener('bob-builder-changed', this.changed);
      }
    };
  }
  private schedule(delay: number) {
    clearTimeout(this.timer);
    if (this.listeners.size) this.timer = setTimeout(() => void this.read(), Math.max(delay, this.next - Date.now()));
  }
  setRecord = (id: string, record: BuilderApproval | null) => {
    this.versions.set(id, (this.versions.get(id) ?? 0) + 1);
    if (record) this.records.set(id, record); else this.records.delete(id);
    this.emit();
  };
  setMode = (mode: BuilderModeState | null) => { this.mode = mode; this.emit(); };
  refresh = () => { if (!this.error) { this.next = 0; this.recordErrors.clear(); } return this.read(); };
  async read(): Promise<void> {
    if (this.flight) return this.flight;
    this.next = Math.max(this.next, retryDeadlines.get(this.client) ?? 0);
    if (document.visibilityState === 'hidden' || Date.now() < this.next) { this.schedule(30000); return; }
    const versions = new Map(this.versions);
    this.flight = (async () => {
      try {
        const mode = await this.client.builderModeGet(this.conversation);
        if (!validBuilderMode(mode, this.conversation)) throw new Error('Invalid server mode record.');
        if (!this.mode || BigInt(mode.revision) >= BigInt(this.mode.revision)) this.mode = mode;
        for (const record of mode.approval_records ?? []) {
          if (!validBuilderApproval(record, this.conversation, record.approval_id)) throw new Error('Invalid server approval record.');
          if (versions.get(record.approval_id) === this.versions.get(record.approval_id)) this.records.set(record.approval_id, record);
        }
        // The unchanged shipping broker supports mode.get, not a new opcode.
        // Pending records ride that batched response. Historical IDs are read
        // once, at most two per tick, and shared by every duplicate card.
        const returned = new Set((mode.approval_records ?? []).map(r => r.approval_id));
        const unknown = [...this.ids.keys()].filter(id => !this.recordErrors.has(id) &&
          (!this.records.has(id) || !builderTerminal(this.records.get(id)!) && !returned.has(id))).slice(0, 2);
        for (const id of unknown) {
          try {
            const record = await this.client.builderApprovalGet(id);
            if (!validBuilderApproval(record, this.conversation, id)) { this.recordErrors.set(id, 'Invalid server approval record.'); continue; }
            if (versions.get(id) === this.versions.get(id)) this.records.set(id, record);
          } catch (e) {
            const code = toPortalError(e).code;
            if (['approval_not_found', 'not_found', 'gateway_http_404', 'forbidden', 'gateway_http_403'].includes(code)) this.recordErrors.set(id, code);
            else throw e;
          }
        }
        this.error = ''; this.fault = null; this.failures = 0;
        this.next = Date.now() + (this.waiting() ? 15000 : 30000);
      } catch (e) {
        const error = toPortalError(e);
        this.fault = error;
        this.error = `Reconnecting · ${error.detail || error.code}`;
        // Native transports that cannot carry Retry-After still get a bounded
        // conservative delay. Focus/online events cannot bypass this deadline.
        const delay = Math.max((error.retry_after ?? (isRateLimited(error) ? 30 : 0)) * 1000, Math.min(120000, 5000 * 2 ** this.failures++));
        this.next = Date.now() + delay + Math.random() * 1000;
        if (isRateLimited(error) || error.retry_after) retryDeadlines.set(this.client, this.next);
      } finally {
        this.flight = null; this.emit();
        // Completed/idle conversations wake on tagged server changes, focus,
        // navigation or an explicit refresh; only live/pending work is polled.
        if (!this.error && (this.live || this.waiting())) this.schedule(0);
        else if (!this.error && this.mode?.mode === 'yolo' && builderTime(this.mode.expires_at) > Date.now()) {
          this.next = builderTime(this.mode.expires_at); this.schedule(0);
        }
      }
    })();
    return this.flight;
  }
}

export function builderReadStore(client: PortalClient, conversation: string, scope: string, connection = '') {
  let entries = stores.get(client);
  if (!entries) { entries = new Map(); stores.set(client, entries); }
  const key = JSON.stringify([scope, conversation, connection]);
  let store = entries.get(key);
  if (!store) { store = new BuilderReadStore(client, conversation); entries.set(key, store); }
  return store;
}
export function useBuilderRead(client: PortalClient, conversation: string, scope: string, connection?: string, id?: string, enabled = true) {
  const store = builderReadStore(client, conversation, scope, connection);
  const [, render] = useState(0);
  useEffect(() => enabled ? store.subscribe(() => render(n => n + 1), id) : undefined, [store, id, enabled]);
  return store;
}
