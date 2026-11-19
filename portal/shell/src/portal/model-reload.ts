import type { NativeInvoke } from '../workbench/workbench-client';
import { toPortalError, type Catalog } from './portal-protocol';

export const MODEL_APPLYING = 'Applying… Bob restarts its model service, a few seconds';
export const MODEL_RETRY_MS = [500, 1000, 2000, 3000, 5000];
const restarts = new WeakMap<NativeInvoke, { generation: number; until: number }>();
export const modelGeneration = (invoke: NativeInvoke) => restarts.get(invoke)?.generation ?? 0;
export const modelRestarting = (invoke: NativeInvoke) => Date.now() < (restarts.get(invoke)?.until ?? 0);
export function beginModelRestart(invoke: NativeInvoke) {
  restarts.set(invoke, { generation: modelGeneration(invoke) + 1, until: Date.now() + 65000 });
}
export function transientModelRead(error: unknown) {
  const { code } = toPortalError(error);
  return code === 'transport_unavailable' || code === 'timeout_no_reply' || /^gateway_http_5\d\d$/.test(code);
}
/** Reads only. Never wrap a save, sign-in, send, Start, or another write. */
export async function retryModelRead<T>(invoke: NativeInvoke, read: () => Promise<T>, always = false): Promise<T> {
  const deadline = Date.now() + 60000;
  for (let attempt = 0; ; attempt++) {
    try { return await read(); }
    catch (error) {
      const remaining = Math.min(deadline, always ? deadline : restarts.get(invoke)?.until ?? 0) - Date.now();
      // Native setup helpers deliberately return fixed safe text rather than
      // upstream error bodies. Retry only these read failures in a known restart.
      const nativeReadFault = !always && modelRestarting(invoke) && [
        'Model status failed', 'Model status task failed', 'ChatGPT model request failed',
        'ChatGPT model list task failed', 'The Claude request failed. Refresh status and try again.',
      ].includes(toPortalError(error).detail);
      if ((!always && !modelRestarting(invoke)) || (!transientModelRead(error) && !nativeReadFault) || remaining <= 0) throw error;
      await new Promise(resolve => setTimeout(resolve, Math.min(remaining, MODEL_RETRY_MS[Math.min(attempt, MODEL_RETRY_MS.length - 1)])));
    }
  }
}

/** The broker can return a partial catalog with HTTP failures in warnings. */
export function checkedModelCatalog(catalog: Catalog): Catalog {
  const warning = catalog.capabilities?.warnings?.find(line =>
    /^(faces|models|backends|capabilities|execution_v2): (transport_unavailable|timeout_no_reply|gateway_http_5\d\d)$/.test(line));
  if (warning) throw { code: warning.split(': ')[1], detail: 'The model catalog could not be loaded. ' + warning };
  return catalog;
}

export type ModelChangedEvent = Event & { catalogRefreshed?: boolean; waitUntil?: (read: Promise<void>) => void };
/** Settings callbacks and event listeners share the same fresh-catalog acknowledgement. */
export async function notifyModelChanged(invoke: NativeInvoke, reload?: () => void | Promise<void>) {
  beginModelRestart(invoke);
  const reads: Promise<void>[] = [];
  window.dispatchEvent(Object.assign(new Event('bob-model-changed'), {
    catalogRefreshed: !!reload, waitUntil: (read: Promise<void>) => { reads.push(read); },
  }));
  if (reload) reads.push(Promise.resolve(reload()));
  await Promise.all(reads);
  // First-run setup has no connected portal/catalog reader yet. Its model-status
  // read still retries; the portal loads its first catalog when Connect is pressed.
}

/** Only the latest request may publish a catalog or an error, including at connect. */
export class CatalogReload<T> {
  private sequence = 0;
  private latest: Promise<void> = Promise.resolve();
  constructor(private read: () => Promise<T>, private accept: (value: T) => void, private fault: (error: unknown | null) => void) {}
  reload = (): Promise<void> => {
    const sequence = ++this.sequence;
    this.fault(null);
    const task = (async () => {
      try {
        const value = await this.read();
        if (sequence !== this.sequence) return this.latest;
        this.accept(value);
      } catch (error) {
        if (sequence !== this.sequence) return this.latest;
        this.fault(error); throw error;
      }
    })();
    this.latest = task; return task;
  };
  invalidate() { this.sequence++; this.latest = Promise.resolve(); }
}
