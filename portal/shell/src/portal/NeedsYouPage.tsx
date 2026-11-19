import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { WorkflowStore } from './workflow-store';
import { useWorkflows, WorkflowRunView, RunAgainControl } from './WorkflowViews';
import { workflowEnded } from './workflow-types';
import { toPortalError } from './portal-protocol';

export type NeedItem = { id: string; kind: string; agent: string; conversation: string; ask: string; since?: string | number | null; observed?: boolean; content: ReactNode };
export function needTime(value?: string | number | null): number {
  return typeof value === 'number' ? value : value ? Date.parse(value) : NaN;
}
export function waitingTime(value: NeedItem['since'], now: number): string {
  const at = needTime(value);
  if (!Number.isFinite(at)) return 'The waiting time was not reported.';
  const minutes = Math.max(0, Math.floor((now - at) / 60000));
  if (!minutes) return 'Waiting for less than a minute.';
  if (minutes < 60) return 'Waiting for ' + minutes + ' minute' + (minutes === 1 ? '' : 's') + '.';
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return 'Waiting for ' + hours + ' hour' + (hours === 1 ? '' : 's') + '.';
  const days = Math.floor(hours / 24);
  return 'Waiting for ' + days + ' day' + (days === 1 ? '' : 's') + '.';
}
export function NeedsYouPage({items, failures, checkedAt, checking, onRefresh, store}: {
  items: NeedItem[]; failures: string[]; checkedAt: number | null; checking: boolean; onRefresh: () => void; store: WorkflowStore;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 60000); return () => clearInterval(timer); }, []);
  return <section className="needs-page" aria-label="Needs you">
    <header><h1>{items.length ? items.length + (items.length === 1 ? ' thing needs you' : ' things need you') : failures.length ? 'Some things could not be checked.' : checking || !checkedAt ? 'Checking what needs you.' : 'Nothing needs you right now.'}</h1>
      <p className="subtle">{checkedAt ? 'Last check on opening or with Check again: ' + new Date(checkedAt).toLocaleString() + '. Live updates also appear here.' : 'The first check has not finished.'} <button type="button" disabled={checking} onClick={onRefresh}>Check again</button></p>
      {failures.map(line => <p role="alert" key={line}>{line}</p>)}
      {!items.length && !store.includeExpired && <p className="subtle">A workflow proposal that is not started within 15 minutes runs out and has to be asked for again.</p>}
    </header>
    {!!items.length && <ol className="needs-list" aria-label="Waiting for you">{[...items].sort((a,b) => (needTime(a.since) || Infinity) - (needTime(b.since) || Infinity) || a.id.localeCompare(b.id)).map(item => <li key={item.id}><article className="bob-action-card need-card" data-need-id={item.id}>
      <h2>{item.kind}</h2><p>{item.conversation === 'From the Library' ? item.conversation : item.agent + ' · ' + item.conversation}</p><p className="need-ask">{item.ask}</p><p className="subtle">{waitingTime(item.since,now)}{item.observed && ' This is measured from when this app first saw the request.'}</p>{item.content}
    </article></li>)}</ol>}
    <Earlier store={store} now={now} />
  </section>;
}
function Earlier({store, now}: {store: WorkflowStore; now: number}) {
  useWorkflows(store);
  const [error,setError] = useState<Record<string,string>>({}), [busy,setBusy] = useState<string | null>(null);
  const lock = useRef(false);
  const [showAll,setShowAll] = useState(false), [selected,setSelected] = useState<string | null>(null);
  const renewed = new Set([...store.proposals.values()].flatMap(({proposal:p}) => p.previous_proposal_id ? [p.previous_proposal_id] : []));
  const cutoff = now - 7 * 86400000;
  const earlier = [
    ...[...store.proposals.values()].filter(({proposal:p}) => store.includeExpired && p.state === 'expired' && !p.run_id && !p.proposed_again_ids?.length && !renewed.has(p.id))
      .map(entry => ({id:'proposal:'+entry.proposal.id,at:needTime(entry.proposal.created_at ?? Date.parse(entry.proposal.expires_at)-15*60000),entry,run:null})),
    // discover() already reads workflows.runs; putRun() also keeps these summaries current from events.
    ...[...store.summaries.values()].filter(r => workflowEnded(r,store.runs.get(r.id)))
      .map(run => ({id:'run:'+run.id,at:needTime(run.updated_at),run,entry:null})),
  ].filter(item => Number.isFinite(item.at) && (!item.run || item.at >= cutoff) && item.at <= now)
    .sort((a,b) => b.at-a.at || a.id.localeCompare(b.id));
  async function again(id: string) {
    if (lock.current) return; lock.current = true; setBusy(id); setError(prior => ({...prior,[id]:''}));
    try {
      const {proposal} = await store.client.workflowProposeAgain(id);
      if (proposal.state !== 'proposed' || proposal.run_id) throw new Error('A fresh proposal was not returned. Nothing has started.');
      const old = store.proposals.get(id)!;
      store.proposals.set(id,{...old,proposal:{...old.proposal,proposed_again_ids:[...(old.proposal.proposed_again_ids ?? []),proposal.id]}});
      store.proposals.set(proposal.id,{proposal:{...proposal,previous_proposal_id:proposal.previous_proposal_id ?? id},source:{...old.source,revision:proposal.revision,digest:proposal.digest}}); store.changed();
    } catch (e) {
      // The broker reports a refusal as its code; say it in words.
      const detail = toPortalError(e).detail;
      const words: Record<string,string> = {
        proposal_recheck_failed: 'The fresh check did not pass, so no new proposal was made. Run the workflow again from the Library to see what changed.',
        workflow_deleted: 'This workflow has been deleted.',
        proposal_not_expired: 'This proposal has already been started or cancelled.',
        proposal_not_found: 'This proposal is no longer on record.',
        proposal_again_not_permitted: 'Only the signed-in owner can ask for this proposal again.',
      };
      setError(prior => ({...prior,[id]:'The proposal could not be asked for again. ' + (words[detail] ?? detail)}));
    }
    finally { lock.current = false; setBusy(null); }
  }
  const outcomes: Record<string,string> = {completed:'This run completed.',failed:'This run failed.',stopped:'This run was stopped.',interrupted:'This run was interrupted and cannot be recovered.',over_budget:'This run ended because its budget was exceeded.',expired:'This run ended because its permission or budget expired.',cancelled:'This run was cancelled.',blocked:'This run ended because it was blocked.',unknown:'This run ended with an unknown outcome.'};
  return earlier.length ? <section aria-label="Earlier"><h2>Earlier</h2>{(showAll ? earlier : earlier.slice(0,5)).map(item => item.run ? <article className={'bob-action-card earlier-run ' + (item.run.status === 'completed' ? 'earlier-completed' : 'earlier-problem')} data-run-id={item.run.id} key={item.id}>
    <h3>{item.run.name}</h3><p className="earlier-outcome">{outcomes[item.run.status]}</p>
    {item.run.reason && <p>{item.run.reason.replaceAll('_',' ').replace(/[.!?]?$/,'.')}</p>}
    <p>Ended on <time dateTime={item.run.updated_at}>{new Date(item.at).toLocaleString()}</time>.</p>
    <button type="button" aria-expanded={selected === item.run.id} onClick={() => setSelected(item.run!.id)}>Open the run</button>
    <RunAgainControl store={store} runId={item.run.id} />
  </article> : item.entry && <article className="bob-action-card" key={item.id}>
    <h3>{item.entry.proposal.coverage.definition.name}</h3><p>Proposed by {item.entry.source.name} on {new Date(item.at).toLocaleString()}.</p>
    <p>This proposal ran out after 15 minutes without being started.</p>
    {store.proposeAgainAdvertised && <button type="button" disabled={busy !== null} onClick={() => void again(item.entry!.proposal.id)}>Ask again</button>}
    {error[item.entry.proposal.id] && <p role="alert">{error[item.entry.proposal.id]}</p>}
  </article>)}
    {earlier.length > 5 && <button type="button" aria-expanded={showAll} onClick={() => setShowAll(value => !value)}>{showAll ? 'Show fewer' : 'Show all'}</button>}
    {selected && <section aria-label="Selected workflow run"><button type="button" onClick={() => setSelected(null)}>Close the run view</button><WorkflowRunView key={selected} id={selected} /></section>}
  </section> : null;
}
