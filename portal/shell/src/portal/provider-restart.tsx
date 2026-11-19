import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import type { PortalClient } from './portal-client';
import { isRateLimited, toPortalError } from './portal-protocol';

type Restart = { label: string; until: number; disconnected: boolean };
const restarts = new WeakMap<PortalClient, Restart>();
const listeners = new Set<() => void>();
const changed = () => listeners.forEach(listener => listener());
export const ProviderRestartClient = createContext<PortalClient | null>(null);

/** Wording only: never schedules a request or replays an operation. */
export function expectProviderRestart(client: PortalClient, label: string) {
  restarts.set(client, { label, until: Date.now() + 120000, disconnected: false });
  changed();
}

export function restartTransportFault(error: unknown) {
  const typed = toPortalError(error);
  // Protocol, permission, rate-limit and application faults are not a restart.
  return ['transport_unavailable', 'timeout_no_reply'].includes(typed.code);
}

export function observeRestartLink(client: PortalClient, healthy: boolean, error?: unknown) {
  const restart = restarts.get(client);
  if (!restart) return;
  if (Date.now() >= restart.until || error && !restartTransportFault(error) || healthy && restart.disconnected) {
    restarts.delete(client); changed();
  } else if (!healthy) restart.disconnected = true;
  // A healthy read immediately after saving precedes the delayed lane restart.
}

export function providerRestartMessage(client: PortalClient, error: unknown): string | null {
  const restart = restarts.get(client);
  return restart && Date.now() < restart.until && restartTransportFault(error)
    ? `Bob is restarting to load ${restart.label}…` : null;
}

export function useProviderRestartMessage(client: PortalClient | undefined, error: unknown) {
  const inherited = useContext(ProviderRestartClient);
  const current = client ?? inherited;
  const restart = current ? restarts.get(current) : undefined;
  const [, render] = useState(0);
  useEffect(() => {
    const update = () => render(n => n + 1);
    listeners.add(update);
    const timer = restart && setTimeout(update, Math.max(0, restart.until - Date.now()));
    return () => { listeners.delete(update); if (timer) clearTimeout(timer); };
  // Re-arm the wording deadline when a new save notifies the component.
  }, [current, restart]);
  return current ? providerRestartMessage(current, error) : null;
}

export function ReadFaultText({ client, error, fallback }: { client?: PortalClient; error: unknown; fallback?: ReactNode }) {
  const message = useProviderRestartMessage(client, error);
  const typed = toPortalError(error);
  if (isRateLimited(typed)) return <>{typed.detail}</>;
  return <>{message ?? fallback ?? toPortalError(error).detail}</>;
}
