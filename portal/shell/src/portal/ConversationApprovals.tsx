import { useEffect, useState } from 'react';
import type { PortalClient } from './portal-client';
import { builderTime } from './portal-protocol';
import { useBuilderRead } from './builder-read-store';

/** Keep background Builder waits in the same read store as both decision cards.
 * This observes receipts only; it never changes permission or grants access. */
export function ConversationApprovals({ client, id, scope, connection, live, onPending }: {
  client: PortalClient; id: string; scope: string; connection: string; live: boolean;
  onPending: (id: string, pending: string[], error?: string) => void;
}) {
  const store = useBuilderRead(client, id, scope, connection);
  useEffect(() => { store.setLive(live); return () => store.setLive(false); }, [store, live]);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const pending = [...store.records.values()].filter(r => r.state === 'pending' && builderTime(r.expires_at) > now).map(r => r.approval_id).sort();
  const signature = JSON.stringify(pending);
  useEffect(() => { if (store.mode || store.error) onPending(id, JSON.parse(signature), store.error); }, [id, onPending, signature, store.mode, store.error]);
  return null;
}
