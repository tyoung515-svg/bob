import {invoke} from '@tauri-apps/api/core';
import WorkbenchApp from './WorkbenchApp';
import type {NativeInvoke} from './workbench/workbench-client';
// Legacy functional entry deliberately uses the unchanged functional bridge.
// The normal main.tsx entry uses WorkbenchApp directly with the extended bridge.
const compatibilityInvoke:NativeInvoke=(command,args)=>command==='workbench_snapshot'?Promise.reject('unknown command workbench_snapshot (legacy functional entry)'):invoke(command,args);
export default function FunctionalApp(){return <WorkbenchApp invoke={compatibilityInvoke}/>}
