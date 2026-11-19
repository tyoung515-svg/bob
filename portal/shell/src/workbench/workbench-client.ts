import type {AppSnapshot} from '../functional-protocol';
import type {WorkbenchSnapshot} from '../workbench-protocol';
export type NativeInvoke = <T = unknown>(command:string,args?:Record<string,unknown>)=>Promise<T>;
// Presentation state only. All runtime entities use generated native types;
// this view is never persisted or submitted as an authority/effect ledger.
export type WorkbenchView = {app:AppSnapshot|null; compatibility:boolean; connection:string; notices:string[]; runtime:WorkbenchSnapshot|null};
export function isApp(value:unknown):value is AppSnapshot {
 return !!value && typeof value==='object' && 'session' in value && 'saved' in value && Array.isArray(value.saved);
}
export function decodeWorkbench(value:unknown):WorkbenchView {
 if(!value||typeof value!=='object'||!('schema_version' in value)||value.schema_version!=='workbench-view-v1'||!('app' in value)||(value.app!==null&&!isApp(value.app)))throw new Error('Invalid workbench snapshot; retaining last known session');
 for(const field of ['profiles','agents','workflows','outputs','notices'])if(!(field in value)||!Array.isArray((value as Record<string,unknown>)[field]))throw new Error(`Invalid workbench ${field}; retaining last known session`);
 const runtime=value as WorkbenchSnapshot;
 return {app:runtime.app,compatibility:false,connection:runtime.connection_state,notices:runtime.notices,runtime};
}
export function createWorkbenchClient(invoke:NativeInvoke){
 async function snapshot():Promise<WorkbenchView>{
  try{return decodeWorkbench(await invoke<unknown>('workbench_snapshot'))}
  catch(error){
   // Only absence of the extension permits compatibility; never mask outages.
   if(!/unknown command|command .*not found|not found.*workbench_snapshot/i.test(String(error)))throw error;
   return {app:await invoke<AppSnapshot>('app_boot'),compatibility:true,connection:'unknown',notices:['Compatibility mode — local app authority; extended runtime views unavailable.'],runtime:null};
  }
 }
 return {snapshot,invoke};
}
