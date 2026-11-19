import { useEffect, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { Catalog, SavedTeam } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { allTeams, canonical, criterionSummary, executionReason, featureReason, FOUR_STEP, LOOP, saveTeamProposal, supportsV2, teamProposal, UPGRADE_REASON } from './durable-runs';
import type { RunSeed } from './DurableRunsPanel';

export function TeamProposalCard({ content, client, catalog, scope, onRun }: {
  content: string; client: PortalClient; catalog: Catalog | null; scope: string; onRun: (seed: RunSeed) => void;
}) {
  const [proposal, setProposal] = useState(() => teamProposal(content));
  useEffect(() => { setProposal(teamProposal(content)); }, [content]);
  const [saved, setSaved] = useState<SavedTeam | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const loopReason = featureReason(catalog, 'loop');
  useEffect(() => {
    setSaved(null);
    if (!proposal || !supportsV2(catalog)) return;
    let live = true;
    void allTeams(client).then(rows => {
      const match = rows.find(row => row.id === proposal.definition.id &&
        Object.entries(proposal.definition).every(([key, value]) => canonical(row[key as keyof SavedTeam]) === canonical(value)));
      if (live && match) setSaved(match);
    }).catch(e => { if (live) { const typed = toPortalError(e); setError(`${typed.code}: ${typed.detail}`); } });
    return () => { live = false; };
  }, [proposal, client, catalog, scope]);
  if (!proposal) return null;
  async function save() {
    if (!proposal) return;
    setBusy(true); setError('');
    try { setSaved(await saveTeamProposal(client, scope, proposal)); }
    catch (e) { const typed = toPortalError(e); setError(`${typed.code}: ${typed.detail}`); }
    finally { setBusy(false); }
  }
  return <section aria-label="Team proposal" className="run-row">
    <h3>{proposal.definition.id} · {saved ? 'Saved and read back' : 'Proposed team'}</h3>
    <p>{proposal.task}</p><p>{proposal.budget.start_calls} calls · {proposal.budget.wall_mode === 'none' ? 'No time limit' : `${proposal.budget.wall_seconds} seconds`}</p>
    <label>Team approach <select aria-label="Team approach" value={proposal.definition.topology} disabled={busy} onChange={e => {
      setProposal({ ...proposal, expected_revision: saved?.revision ?? proposal.expected_revision, definition: { ...proposal.definition, topology: e.target.value } }); setSaved(null);
    }}>
      <option value={FOUR_STEP}>Quick reviewed answer · four-step</option>
      <option value={LOOP} disabled={!!loopReason}>Multi-part work · loop</option>
    </select></label>
    {loopReason && <p role="status">{loopReason}</p>}
    {typeof proposal.acceptance === 'object' && proposal.acceptance.criteria.map(c => <p key={c.id}>{c.required ? 'Required' : 'Optional'}: {criterionSummary(c)}</p>)}
    <p>{Object.entries(proposal.definition.roles).map(([role, binding]) => `${role}: ${binding ?? 'unresolved'}`).join(' · ')}</p>
    {saved && <p>Saved revision {saved.revision} · {saved.runnable ? 'runnable' : 'blocked'} · {saved.blocked_reasons.join(', ')}</p>}
    {!supportsV2(catalog) && <p role="status">{UPGRADE_REASON}</p>}
    {error && <p role="alert">{error}</p>}
    <button disabled={busy || !!saved || !supportsV2(catalog) || (proposal.definition.topology === LOOP && !!loopReason)} onClick={() => void save()}>{busy ? 'Saving and reading back…' : 'Save team'}</button>
    <p>Saving never starts a run or enables a schedule.</p>
    {saved && <button disabled={!saved.runnable || !!executionReason(catalog)} onClick={() => onRun({ team: saved.id, task: proposal.task, budget: proposal.budget, job_time: proposal.job_time, job_coverage: proposal.job_coverage, evidence_ids: proposal.evidence_ids, acceptance: proposal.acceptance, at: Date.now() })}>Review run budget</button>}
  </section>;
}
