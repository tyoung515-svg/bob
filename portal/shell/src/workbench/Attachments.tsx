import {useEffect,useRef,useState,type DragEvent} from 'react';
export type StagedFile={name:string;mime:string;bytes:number[];image:boolean;preview?:string;id:string};
export function AttachmentPicker({files,onFiles,locked,onError,sessionId}:{files:StagedFile[];onFiles:(files:StagedFile[],sessionId:string)=>void;locked:boolean;onError:(error:string)=>void;sessionId:string}){
 const [expanded,setExpanded]=useState(false);
 const picker=useRef<HTMLInputElement>(null),loading=useRef(false);
 async function add(selected:File[]){
  if(locked||loading.current||!selected.length)return;
  loading.current=true;const target=sessionId;
  try{
   if(files.length+selected.length>4)throw Error('At most 4 attachments per draft');
   const staged:StagedFile[]=[];
   for(const file of selected){
    if(!file.name||file.name.length>180||/[\\/\x00-\x1f]/.test(file.name))throw Error('Invalid filename');
    const image=/\.(png|jpe?g)$/i.test(file.name);
    if(file.size>(image?2097152:16384))throw Error(image?'Image staging maximum is 2MiB':'Text attachment maximum is 16KiB');
    if(!image&&!/\.(txt|md|markdown|csv|json|rs|py|js|ts|tsx|jsx|css|toml|yaml|yml|log)$/i.test(file.name))throw Error('Unsupported file format. Use UTF-8 text/code, PNG or JPEG staging.');
    const buffer=await new Promise<ArrayBuffer>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result as ArrayBuffer);reader.onerror=()=>reject(Error('File read failed'));reader.readAsArrayBuffer(file)});
    const bytes=Array.from(new Uint8Array(buffer));let preview:string|undefined;
    if(image){
     const png=bytes.length>=24&&bytes.slice(0,8).join(',')==='137,80,78,71,13,10,26,10',jpeg=bytes.length>=4&&bytes[0]===255&&bytes[1]===216&&bytes[2]===255;
     if(!png&&!jpeg)throw Error('Image magic does not match PNG/JPEG');
     if((png&&!/\.png$/i.test(file.name))||(jpeg&&!/\.jpe?g$/i.test(file.name)))throw Error('Image filename/magic mismatch');
     if(png){const v=new DataView(buffer);if(!v.getUint32(16)||!v.getUint32(20)||v.getUint32(16)>4096||v.getUint32(20)>4096)throw Error('Preview dimension limit 4096px');}
     if(jpeg){
      let offset=2,found=false;
      while(offset+4<=bytes.length){
       if(bytes[offset]!==255)break;
       while(bytes[offset+1]===255)offset++;
       const marker=bytes[offset+1];offset+=2;
       if(marker===217||marker===218)break;
       if(marker===1||(marker>=208&&marker<=215))continue;
       const length=(bytes[offset]<<8)|bytes[offset+1];if(length<2||offset+length>bytes.length)break;
       if([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(marker)){
        if(length<8)break;
        const h=(bytes[offset+3]<<8)|bytes[offset+4],w=(bytes[offset+5]<<8)|bytes[offset+6];
        if(!w||!h||w>4096||h>4096)throw Error('Preview dimension limit 4096px');found=true;
       }
       offset+=length;
      }
      if(!found)throw Error('JPEG dimensions could not be validated before preview');
     }
     preview=await new Promise<string>((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result));r.onerror=()=>reject(Error('Preview failed'));r.readAsDataURL(new Blob([buffer],{type:png?'image/png':'image/jpeg'}))});
    }
    staged.push({id:crypto.randomUUID(),name:file.name,mime:file.type,bytes,image,preview});
   }
   const all=[...files,...staged];if(all.filter(f=>!f.image).reduce((n,f)=>n+f.bytes.length,0)>32768||all.reduce((n,f)=>n+f.bytes.length,0)>4194304)throw Error('Draft attachment total limit exceeded');
   onFiles(all,target);onError('');
  }catch(e){onError(String(e))}finally{loading.current=false;if(picker.current)picker.current.value=''}
 }
 function drop(e:DragEvent){e.preventDefault();void add(Array.from(e.dataTransfer.files))}
 function paste(e:globalThis.ClipboardEvent){if(!e.clipboardData)return;const images=Array.from(e.clipboardData.items).filter(i=>i.kind==='file'&&i.type.startsWith('image/')).flatMap(i=>{const f=i.getAsFile();return f?[f]:[]});if(images.length){e.preventDefault();void add(images)}}
 useEffect(()=>{
  const handler=(e:globalThis.ClipboardEvent)=>{if(e.target instanceof HTMLElement&&e.target.id==='composer')paste(e)};
  document.addEventListener('paste',handler);return()=>document.removeEventListener('paste',handler);
 },[files,locked,sessionId]);
 return <section className="attachments" aria-label="Draft attachments" onDragOver={e=>e.preventDefault()} onDrop={drop}>
  <input ref={picker} type="file" multiple hidden aria-label="Choose attachments" accept=".txt,.md,.markdown,.csv,.json,.rs,.py,.js,.ts,.tsx,.jsx,.css,.toml,.yaml,.yml,.log,.png,.jpg,.jpeg" onChange={e=>void add(Array.from(e.target.files??[]))}/>
  <button type="button" disabled={locked} onClick={()=>picker.current?.click()}>Add files</button><button type="button" aria-expanded={expanded} aria-controls="draft-attachment-details" onClick={()=>setExpanded(!expanded)}>{expanded?'Hide':'Show'} attachment details ({files.length})</button>
  <div id="draft-attachment-details" hidden={!expanded}><small>Drop files here or paste an image. Selected bytes only; no folder access.</small>
  {files.map((f,i)=><div key={f.id} className="attachment"><span>{f.name} · {f.bytes.length} bytes · {f.mime||'type unknown'}</span>{f.preview&&<img src={f.preview} alt={`Staged preview: ${f.name}`} width={100} height={80}/>}<button type="button" disabled={locked} onClick={()=>onFiles(files.filter((_,n)=>n!==i),sessionId)}>Remove {f.name}</button></div>)}
  </div>{files.some(f=>f.image)&&<p role="status">Current backend has no vision; image not sent/understood. Preview only. Remove images to send text attachments.</p>}
 </section>
}
