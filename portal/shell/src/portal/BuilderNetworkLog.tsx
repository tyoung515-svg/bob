import { ReadFaultText } from './provider-restart';
import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { BuilderNetworkLogEntry, BuilderNetworkLogQuery } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { builderHostname, builderMethod, builderRevision } from './builder-network';

export function BuilderNetworkLog({ client, conversationId, jobId, refreshKey }: {
  client: PortalClient; conversationId?: string; jobId?: string; refreshKey?: string;
}) {
  const [entries, setEntries] = useState<BuilderNetworkLogEntry[] | null>(null);
  const [selectedJob, setSelectedJob] = useState('');
  const [error, setError] = useState('');
  const [truncated, setTruncated] = useState(false);
  const epoch = useRef(0), sequence = useRef(0);
  async function read(generation: number) {
    const started = ++sequence.current;
    try {
      if (!!conversationId === !!jobId) throw new Error('Choose one conversation or job.');
      const query: BuilderNetworkLogQuery = jobId ? { job_id: jobId } : { conversation_id: conversationId! };
      const value = await client.builderNetworkLog(query);
      if (typeof value?.truncated !== 'boolean' || !Array.isArray(value.items) || value.items.length > 500 || value.items.some(row => !row || typeof row.job_id !== 'string' || !row.job_id ||
        (jobId && row.job_id !== jobId) || (conversationId && row.conversation_id !== conversationId) || !builderHostname(row.domain) ||
        (row.method != null && !builderMethod(row.method)) || !builderRevision(row.bytes_in) || !builderRevision(row.bytes_out) ||
        typeof row.decision !== 'string' || !row.decision || (row.approval_id != null && typeof row.approval_id !== 'string'))) throw new Error('Invalid server network log.');
      if (generation === epoch.current && started === sequence.current) { setEntries(value.items); setTruncated(value.truncated); setError(''); }
    } catch (e) { if (generation === epoch.current && started === sequence.current) { setEntries(null); setError(JSON.stringify(toPortalError(e))); } }
  }
  useEffect(() => {
    const generation = ++epoch.current; setEntries(null); setError(''); setSelectedJob('');
    const refresh = () => void read(generation);
    refresh(); window.addEventListener('focus', refresh); window.addEventListener('online', refresh);
    return () => { epoch.current++; window.removeEventListener('focus', refresh); window.removeEventListener('online', refresh); };
  }, [client, conversationId, jobId, refreshKey]);
  const jobs = [...new Set(entries?.map(row => row.job_id))];
  const shown = entries?.filter(row => !selectedJob || row.job_id === selectedJob);
  return <section aria-label="Builder network log" style={{ overflow: 'auto' }}>
    <h4>Network log</h4>
    {!jobId && <label>Job <select aria-label="Network log job" value={selectedJob} onChange={e => setSelectedJob(e.target.value)}><option value="">All jobs</option>{jobs.map(id => <option key={id} value={id}>{id}</option>)}</select></label>}
    <button type="button" onClick={() => void read(epoch.current)}>Refresh network log</button>
    {shown && (shown.length ? <table><thead><tr>{['Job', 'Approval', 'Domain', 'Method', 'Bytes in', 'Bytes out', 'Decision'].map(s => <th key={s}>{s}</th>)}</tr></thead><tbody>{shown.map((row, i) => <tr key={i}><td>{row.job_id}</td><td>{row.approval_id ?? '—'}</td><td>{row.domain}</td><td>{row.method ?? 'not visible (HTTPS CONNECT)'}</td><td>{row.bytes_in}</td><td>{row.bytes_out}</td><td>{row.decision}</td></tr>)}</tbody></table> : <p>No proxied requests recorded.</p>)}
    <p>HTTPS CONNECT hides the request method. Method limits apply to plain HTTP and are best effort for read-only hosts.</p>
    {entries && truncated && <p>Showing the latest 500 requests. Older requests are omitted.</p>}
    {error && <p role="alert"><ReadFaultText client={client} error={error} /></p>}
  </section>;
}
