import {useState} from 'react';
import type {AppSnapshot,OrganizationalProject} from '../functional-protocol';
type Action=(command:string,args:Record<string,unknown>)=>Promise<boolean>;
export function ProjectNavigation({app,filter,onFilter,search,locked,onAction}:{app:AppSnapshot;filter:string;onFilter:(id:string)=>void;search:string;locked:boolean;onAction:Action}){
 const [editor,setEditor]=useState<{id:string|null;version:number|null;name:string;description:string;instructions:string}|null>(null);
 const projects=app.organizational_projects??[];
 const selected=projects.find(p=>p.id===filter)||projects.find(p=>p.id===app.group_id);
 const matches=projects.filter(p=>`${p.name} ${p.description}`.toLowerCase().includes(search.toLowerCase()));
 return <section aria-label="Organizational projects" className="organizational-projects"><h2>Named projects <small>{projects.length}</small></h2>
  <p className="subtle">Groups and instructions only — not directory grants or runtime owners.</p>
  <button disabled={locked} onClick={()=>setEditor({id:null,version:null,name:'',description:'',instructions:''})}>New project</button>
  <button disabled={locked||!selected} onClick={()=>selected&&setEditor({id:selected.id,version:selected.version,name:selected.name,description:selected.description,instructions:selected.instructions})}>Edit project instructions</button>
  <div className="project-browse"><button aria-pressed={filter===''} onClick={()=>onFilter('')}>All named projects</button><button aria-pressed={filter===':unfiled'} onClick={()=>onFilter(':unfiled')}>Unfiled conversations</button>
  {matches.map(p=><div role="group" aria-label={`Project: ${p.name}`} key={p.id}><button aria-label={`Browse ${p.name}`} aria-pressed={filter===p.id} onClick={()=>onFilter(p.id)}>Browse {p.name}</button><button disabled={locked} aria-expanded={!p.collapsed} onClick={()=>void onAction('workbench_collapse_project',{projectId:p.id,collapsed:!p.collapsed})}>{p.collapsed?'Expand':'Collapse'} {p.name}</button><small>{p.description||'No description'} · v{p.version}</small></div>)}</div>
  {editor&&<form aria-label="Project editor" className="project-editor" onSubmit={e=>{e.preventDefault();if(!locked&&editor.name.trim())void onAction('workbench_save_project',{projectId:editor.id,expectedVersion:editor.version,name:editor.name,description:editor.description,instructions:editor.instructions}).then(ok=>{if(ok)setEditor(null)})}}>
   <h3>{editor.id?'Edit named project':'Create named project'}</h3>
   <label htmlFor="organizational-project-name">Project name</label><input id="organizational-project-name" value={editor.name} maxLength={160} disabled={locked} onChange={e=>setEditor({...editor,name:e.target.value})}/>
   <label htmlFor="organizational-project-description">Project description</label><textarea id="organizational-project-description" value={editor.description} maxLength={2048} rows={2} disabled={locked} onChange={e=>setEditor({...editor,description:e.target.value})}/>
   <label htmlFor="organizational-project-instructions">Persistent project instructions</label><textarea id="organizational-project-instructions" value={editor.instructions} maxLength={8192} rows={5} disabled={locked} onChange={e=>setEditor({...editor,instructions:e.target.value})}/>
   <small>Native byte limits: name 160, description 2048, instructions 8192. Applies to later turns only. Saved history stays unchanged; instructions grant no tools, paths or approvals.</small>
   <button disabled={locked||!editor.name.trim()}>Save project</button><button type="button" disabled={locked} onClick={()=>setEditor(null)}>Cancel project edit</button>
  </form>}
 </section>
}
export function CurrentProjectContext({app,locked,bobOwned,onAction}:{app:AppSnapshot;locked:boolean;bobOwned:boolean;onAction:Action}){
 const projects=app.organizational_projects??[],current=projects.find(p=>p.id===app.group_id);
 const last=app.session.receipts[app.session.receipts.length-1] as {project_context?:{name?:string;version?:number;digest?:string}|null}|undefined;
 return <section aria-label="Current project context" className="current-project-context">
  {bobOwned&&<p>Local conversation context only — not sent to Bob-owned runs. Switch to an idle local runtime to edit or move groups.</p>}
  <label htmlFor="conversation-project">Organizational project</label><select id="conversation-project" value={app.group_id??''} disabled={locked} onChange={e=>void onAction('workbench_move_session_group',{sessionId:app.session.id,groupId:e.target.value||null})}><option value="">Unfiled — no project instructions</option>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select>
  <p>Named project: <strong>{current?.name??'Unfiled'}</strong> · Filesystem root: <code>{app.session.project?.root??'None'}</code></p>
  {current?<details><summary>{bobOwned?'Saved local project instructions':'Next-turn project instructions'} · v{current.version} · {current.digest}</summary><p>{current.description}</p><pre>{current.instructions||'No instructions'}</pre></details>:<small>No project instructions apply to the next turn.</small>}
  <small>Moving groups does not change root, model, effect owner or approval authority. Earlier context remains historical in this conversation; a move is not a privacy reset.</small>
  {last&&'project_context' in last&&<p>Last applied context: {last.project_context?`${last.project_context.name} · v${last.project_context.version} · ${last.project_context.digest}`:'Unfiled'}. Earlier receipts are unchanged.</p>}
 </section>
}
export function groupName(projects:OrganizationalProject[],id:string|null|undefined){return projects.find(p=>p.id===id)?.name??'Unfiled'}
