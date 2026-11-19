const fs=require('node:fs');
(async()=>{
const root=(process.env.BOB_VALIDATION_ROOT || require('node:path').resolve(__dirname, '../..'));const meta=JSON.parse(fs.readFileSync(root+'/sprint-evidence/native-gui-v2/debug-port.json','utf8').replace(/^\uFEFF/,''));
const targets=await(await fetch(`http://127.0.0.1:${meta.port}/json/list`)).json();const target=targets.find(t=>t.type==='page'&&/tauri\.localhost/.test(t.url));if(!target)throw Error('candidate native WebView target missing: '+JSON.stringify(targets));
const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((ok,no)=>{ws.onopen=ok;ws.onerror=no});let id=0;const pending=new Map();ws.onmessage=e=>{const v=JSON.parse(e.data);const p=pending.get(v.id);if(p){pending.delete(v.id);v.error?p.reject(v.error):p.resolve(v.result)}};
const cdp=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}))});
let expression;
if(process.argv[2]==='eval')expression=fs.readFileSync(process.argv[3],'utf8');
else expression=`JSON.stringify({title:document.title,url:location.href,text:document.body.innerText,buttons:[...document.querySelectorAll('button')].map(x=>({text:x.innerText,label:x.getAttribute('aria-label'),disabled:x.disabled})),inputs:[...document.querySelectorAll('input,textarea,select')].map(x=>({tag:x.tagName,type:x.type,id:x.id,placeholder:x.getAttribute('placeholder'),label:x.getAttribute('aria-label'),value:x.value})),ipc:!!window.__TAURI_INTERNALS__})`;
const result=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));console.log(typeof result.result.value==='string'?result.result.value:JSON.stringify(result.result.value));ws.close();
})().catch(e=>{console.error(e);process.exitCode=1});
