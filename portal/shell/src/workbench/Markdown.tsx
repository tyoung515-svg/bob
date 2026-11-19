import {Fragment,useState,type ReactNode} from 'react';
// Deliberately small Markdown subset. React escapes all text. No HTML parsing,
// image tags, URL fetching, embeds, or model-supplied hrefs are ever produced.
function inline(text:string):ReactNode[]{
 return text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g).map((part,i)=>part.startsWith('`')?<code key={i}>{part.slice(1,-1)}</code>:part.startsWith('**')?<strong key={i}>{part.slice(2,-2)}</strong>:<Fragment key={i}>{part.replace(/!\[([^\]]*)\]\([^)]*\)/g,'[Image disabled: $1]')}</Fragment>);
}
function CodeBlock({text,language}:{text:string;language:string}){
 const [status,setStatus]=useState('Copy code');
 return <div className="code-block"><div><span>{language||'code'}</span><button type="button" onClick={()=>{if(!navigator.clipboard){setStatus('Copy unavailable');return}void navigator.clipboard.writeText(text).then(()=>setStatus('Copied'),()=>setStatus('Copy failed'))}}>{status}</button></div><pre><code>{text}</code></pre></div>;
}
export function Markdown({text}:{text:string}){
 const lines=text.split('\n'),nodes:ReactNode[]=[];
 for(let i=0;i<lines.length;){
  const line=lines[i];
  if(line.startsWith('```')){
   const language=line.slice(3),body:string[]=[];i++;
   while(i<lines.length&&!lines[i].startsWith('```'))body.push(lines[i++]);
   if(i<lines.length)i++;
   nodes.push(<CodeBlock key={i} language={language} text={body.join('\n')+(body.length?'\n':'')}/>);
  }else if(/^#{1,3} /.test(line)){
   nodes.push(<h3 key={i}>{inline(line.replace(/^#{1,3} /,''))}</h3>);i++;
  }else if(/^[-*] /.test(line)){
   const items:ReactNode[]=[];while(i<lines.length&&/^[-*] /.test(lines[i])){items.push(<li key={i}>{inline(lines[i].slice(2))}</li>);i++}nodes.push(<ul key={i}>{items}</ul>);
  }else if(line.trim()){
   const paragraph:string[]=[];while(i<lines.length&&lines[i].trim()&&!/^```|^#{1,3} |^[-*] /.test(lines[i]))paragraph.push(lines[i++]);nodes.push(<p key={i}>{inline(paragraph.join('\n'))}</p>);
  }else i++;
 }
 return <div className="markdown">{nodes}</div>;
}
export function isToolActivity(content:string){
 return content.startsWith('APP TOOL RESULT') || /^\s*\{\s*"(?:tool|tool_calls|name)"\s*:/.test(content) || /^\s*\{\s*"type"\s*:\s*"action"\s*,\s*"action"\s*:/.test(content);
}
export function ConversationMessage({role,content,provenance}:{role:string;content:string;provenance?:string}){
 const source=provenance??(role==='user'?'human':role);
 if(['tool','system','assistant_control','unknown'].includes(source))return <details className="tool-activity"><summary>{source==='tool'?'Local tool evidence':source==='system'?'System activity':source==='assistant_control'?'Assistant control activity':'Unattributed activity'} · inspect exact text</summary><pre>{content}</pre></details>;
 return <article className={`message ${source==='assistant'?'assistant':'user'}`}><h2>{source==='assistant'?'Assistant':source==='human'?'You':source}</h2><Markdown text={content}/></article>;
}
