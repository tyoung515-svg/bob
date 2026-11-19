import type {Event} from '../functional-protocol';
// Presentation only: source events are observed durable native facts, never instructions.
export function TurnActivity({events,busy,preparing,pending,error,notice}:{events:Event[];busy:boolean;preparing:boolean;pending:boolean;error:string;notice:string}){
 let start=0;for(let i=events.length-1;i>=0;i--)if(events[i].kind==='user.message'){start=i;break}
 const calls=new Map<string,string>(),rows:{id:string;text:string}[]=[];
 for(const e of events.slice(start)){
  const d=(e.data&&typeof e.data==='object'?e.data:{}) as Record<string,unknown>;
  const id=typeof d.call_id==='string'?d.call_id:'';
  if(e.kind==='tool.call'){const a=d.action as {tool?:unknown}|undefined;calls.set(id,typeof a?.tool==='string'?a.tool:'Unknown tool')}
  const tool=calls.get(id)??'Tool';let text='';
  switch(e.kind){
   case 'model.requested':text='Waiting for model response';break;
   case 'model.completion':text='Model response received';break;
   case 'model.response_rejected':text=`Response rejected — protocol repair requested; no action executed (${String(d.error??'invalid strict JSON')})`;break;
   case 'tool.call':text=`${tool} proposed`;break;
   case 'tool.started':text=`${tool} authorized to attempt — start witness, not proof of execution`;break;
   case 'tool.result':text=`${tool} ${String(d.status??'unknown')}${typeof d.error==='string'?`: ${d.error}`:''}`;break;
   case 'approval.requested':text='Waiting for human approval';break;
   case 'approval.resolved':text=d.allow===true?'Write approved once':'Write denied';break;
   case 'turn.cancelled':text='Turn cancelled — completed effects are not rolled back';break;
   case 'assistant.message':text='Response completed';break;
  }
  if(text)rows.push({id:e.event_id,text});
 }
 if(!rows.length&&!busy&&!pending&&!error&&!notice)return null;
 const latest=error?`Request failed: ${error}`:pending?'Waiting for human approval':preparing?'Submitting to native app — awaiting acceptance':rows[rows.length-1]?.text??(busy?'Waiting for native observation':'No new activity');
 return <section className="turn-activity" aria-label="Observed turn activity"><p role="status" aria-live="polite">{latest}</p>{notice&&<p>{notice}</p>}{rows.length>0&&<details><summary>Observed activity ({rows.length})</summary><ol>{rows.map(row=><li key={row.id} data-source-event={row.id}>{row.text}</li>)}</ol></details>}</section>;
}
