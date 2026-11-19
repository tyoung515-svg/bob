import { useEffect, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { Catalog, GrantProposal, WireInteger } from './portal-protocol';
import { toPortalError, txt } from './portal-protocol';
import { issueApproval, useOwnerAction } from './OwnerControls';
import { mutationKey, pendingMutation } from './durable-runs';

type Action = { operation: 'work.workspace.create'|'grants.issue'|'work.publish'; resource?: { project_id?: string; job_id?: string }; preparation_id?: string };
type Preparation = { preparation_id: string; target: string; project_id: string; task: string; recipe: string; proposal: GrantProposal };
type WorkStatus = { id: string; run_id: string; status: string; publication?: { state: string; receipt: { code?: string; detail?: unknown } | null } };
type Proposal = { job_id: string; digest: string; output_digest?: string; revision: WireInteger; diff: string; checks_passed: boolean; check_runs: unknown[]; stale_paths: string[]; diff_truncated: boolean };

export function parseWorkAction(content: string): Action | null {
  const raw = /```bob-work-action\s*\n([\s\S]*?)```/.exec(content)?.[1];
  if (!raw || raw.length > 4096) return null;
  try {
    const a = JSON.parse(raw);
    if (a.schema_version !== 1 || !['work.workspace.create','grants.issue','work.publish'].includes(a.operation)) return null;
    if (a.operation === 'grants.issue' && !/^[a-f0-9]{64}$/.test(a.preparation_id)) return null;
    if (a.operation === 'work.publish' && !/^[a-f0-9]{32}$/.test(a.resource?.job_id)) return null;
    if (a.operation === 'work.workspace.create' && !/^[a-f0-9-]{36}$/.test(a.resource?.project_id)) return null;
    return a;
  } catch { return null; }
}

export function WorkActionCard({ content, client, catalog, scope, onContinue }: { content: string; client: PortalClient; catalog: Catalog|null; scope: string; onContinue: (text: string) => void }) {
  const action = parseWorkAction(content);
  const owner = useOwnerAction(client,catalog);
  const [prepared,setPrepared] = useState<Preparation|null>(null);
  const [project,setProject] = useState<{ id: string; name: string }|null>(null);
  const [proposal,setProposal] = useState<Proposal|null>(null);
  const [status,setStatus] = useState<WorkStatus|null>(null);
  const [receipt,setReceipt] = useState<unknown>(null);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const serialized=JSON.stringify(action);
  useEffect(() => {
    let live=true;setPrepared(null);setProject(null);setProposal(null);setStatus(null);setReceipt(null);setError('');
    if (!action) return;
    const load=async () => {
      try {
        if (action.operation==='work.workspace.create') {
          const c=await client.work<{projects: {id: string; name: string}[]}>('catalog');
          const p=c.projects.find(p => p.id===action.resource!.project_id && typeof p.name==='string');
          if (!p) throw new Error('Project is not in your server catalog.');
          if(live)setProject(p);
        } else if (action.operation==='grants.issue') {
          const p=await client.work<Preparation>('preparation',{preparation_id:action.preparation_id});
          if (p.preparation_id!==action.preparation_id || p.target!=='bob-lane-workspace' || !p.proposal?.proposal_id) throw new Error('Invalid server preparation');
          if(live)setPrepared(p);
        } else if(action.operation==='work.publish') {
          const p=await client.jobs.v2.proposal(action.resource!.job_id!);
          if(live)setProposal(p as Proposal);
          const s=await client.work<WorkStatus>('status',{job_id:action.resource!.job_id});
          if(live)setStatus(s);
        }
      } catch(e) {if(live)setError(txt(toPortalError(e).detail));}
    };
    void load();return () => {live=false;};
  },[client,scope,serialized]);
  async function perform(fn: () => Promise<unknown>) {
    setBusy(true);setError('');
    try {setReceipt(await fn());} catch(e) {setError(txt(toPortalError(e).detail) || txt(e));} finally {setBusy(false);}
  }
  async function start() {
    const result=await client.work<WorkStatus>('start',{preparation_id:prepared!.preparation_id,
      request_key:mutationKey(scope,'work-start:'+prepared!.preparation_id,{preparation_id:prepared!.preparation_id})});
    setStatus(result);
    onContinue(`Track BoB managed work job ${result.id}, run ${result.run_id}; read its real status and continue through checks and publication.`);
    return result;
  }
  if(!action)return null;
  const publicationState=status?.publication?.state;
  return <section className="set-card" aria-label="BoB managed work">
    <h3>{action.operation==='work.workspace.create'?'Set up project workspace':action.operation==='grants.issue'?'Review build or fix':'Review checked files'}</h3>
    <p>Target: BoB-managed lane workspace · Python sources and tests under app/.</p>
    {action.operation==='work.workspace.create' && project?.id===action.resource!.project_id && <>
      <p>Project: {project!.name}. Creates a registered workspace for this project.</p>
      <button disabled={busy || !!receipt} onClick={() => void perform(async () => {
        const result=await owner.run('work.workspace.create',{project_id:action.resource!.project_id!},() => client.work('workspace.create',{
          project_id:action.resource!.project_id,request_key:mutationKey(scope,'work-space:'+action.resource!.project_id,{project_id:action.resource!.project_id})}));
        onContinue(`The BoB lane workspace for project ${action.resource!.project_id} is ready. Repeat the same work preparation and continue.`);return result;
      })}>Create project workspace</button>
    </>}
    {prepared && <>
      <p>{prepared.task}</p><p>Project: {prepared.project_id} · Recipe: {prepared.recipe}</p>
      <details><summary>Server authorization request</summary><pre>{txt(prepared.proposal)}</pre></details>
      <button disabled={busy || !!status} onClick={() => void perform(start)}>Start with existing approval</button>
      <button disabled={busy || !!status} onClick={() => void perform(() => owner.run('grants.issue',{proposal_id:prepared.proposal.proposal_id},async policy => {
        await issueApproval(client,scope,prepared.proposal,policy!);return start();
      },prepared.proposal,'Allow and start',scope,{allow_extended_jobs:false,allow_custom_job_limit:false,job_seconds_ceiling:900,job_auto_escalate:false}))}>Review permission and start</button>
    </>}
    {proposal && <>
      <p>Checked snapshot{proposal.output_digest ? ` ${proposal.output_digest.slice(0, 12)}` : ''} · revision {proposal.revision}. Checks describe this snapshot; publication applies it to the workspace.</p>
      <details><summary>Changes · {proposal.diff.split('\n').length} lines</summary><pre>{proposal.diff}</pre></details>
      <p>{proposal.checks_passed ? 'Checks passed on this revision' : 'Checks have not passed on this revision'}</p>
      <CheckReceipts rows={proposal.check_runs} />
      <p>{status?.publication?.state==='completed' ? 'Published and recorded in this workspace.' : proposal.stale_paths.length ? 'Source changed: '+proposal.stale_paths.join(', ') : 'Server source snapshot matches the proposal base.'}</p>
      {publicationState==='unknown' && <p>Outcome unknown — Retry publication reconciles with the same key</p>}
      {publicationState==='conflict' && <p role="alert">Publication conflict: {status?.publication?.receipt?.code || 'publication_conflict'}{status?.publication?.receipt?.detail ? ' · '+txt(status.publication.receipt.detail) : ''}</p>}
      {proposal.diff_truncated && <p>Diff display is truncated. Inspect the job artifacts before publishing.</p>}
      <button disabled={busy || !proposal.checks_passed || (!!proposal.stale_paths.length && publicationState!=='unknown') || status?.status!=='completed' || publicationState==='completed' || publicationState==='conflict'} onClick={() => void perform(() => owner.run('work.publish',{job_id:proposal.job_id},async () => {
        if (publicationState==='unknown' && !pendingMutation(scope,'work-publish:'+proposal.job_id)) {
          throw new Error('Original publication request key is unavailable. Reopen the original client to reconcile this publication.');
        }
        let result;
        try {
          result=await client.work('publish',{job_id:proposal.job_id,proposal_digest:proposal.digest,expected_revision:proposal.revision,
            request_key:mutationKey(scope,'work-publish:'+proposal.job_id,{job_id:proposal.job_id,proposal_digest:proposal.digest,expected_revision:proposal.revision})});
        } finally {
          const fresh=await client.work<WorkStatus>('status',{job_id:proposal.job_id});setStatus(fresh);
        }
        onContinue(`Read the publication receipt for BoB managed job ${proposal.job_id} and report the actual files and checks.`);return result;
      }))}>{publicationState==='unknown' ? 'Retry publication' : 'Publish checked files'}</button>
    </>}
    {status && <p>Job {status.id} · {status.status}{status.publication ? ' · Publication: '+status.publication.state : ''}</p>}
    {receipt!==null && <details><summary>Details · Server receipt</summary><pre>{JSON.stringify(receipt, null, 2)}</pre></details>}
    {error && <p role="alert">{error}</p>}
    {owner.dialog}
  </section>;
}

export function CheckReceipts({ rows }: { rows: unknown[] }) {
  return <details><summary>Check receipts</summary>
    <table><thead><tr><th>Check</th><th>Result</th><th>Exit code</th><th>Revision</th></tr></thead>
      <tbody>{rows.map((value, index) => {
        const row = value && typeof value === 'object' ? value as Record<string, unknown> : {};
        return <tr key={index}><td>{String(row.check_id ?? row.id ?? `Check ${index + 1}`)}</td>
          <td>{row.passed === true ? 'Passed' : row.passed === false ? 'Failed' : 'No verified result'}</td>
          <td>{String(row.exit_status ?? row.exit_code ?? (row.result as Record<string, unknown> | undefined)?.exit_code ?? 'Not reported')}</td>
          <td>{typeof row.snapshot_digest === 'string' ? row.snapshot_digest.slice(0, 12) : 'Not reported'}</td></tr>;
      })}</tbody></table>
    <details><summary>Full diagnostics</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(rows, null, 2)}</pre></details>
  </details>;
}
