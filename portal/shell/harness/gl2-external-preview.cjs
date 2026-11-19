const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../src-tauri/src/portal.rs'),'utf8');
const csp=/const RUNNABLE_EXTERNAL_PREVIEW_CSP: &str = "([^"]+)"/.exec(source)[1];
const format=/format!\("([^\n]+)", builder_html_escape\(&framed\)\)/.exec(source)[1];
const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
// Decode the emitted Rust literal and substitute the exact two format arguments.
const template=JSON.parse('"'+format+'"');
const shim=/const PREVIEW_STORAGE_SHIM: &str = r#"([\s\S]*?)"#;/.exec(source)[1];
const framed=page=>page.replace(/^(\s*<!doctype[^>]*>)?/i,match=>match+shim);
const wrapper=page=>template.replace('{csp}',escape(csp)).replace('{}',()=>escape(framed(page)));
const hostile='"><script>parent.escape=1</script><img onerror="run()">&';
const document=new JSDOM(wrapper(hostile)).window.document;
assert.equal(document.querySelectorAll('iframe').length,1);assert.equal(document.querySelectorAll('script').length,0);
assert.equal(document.querySelector('iframe').getAttribute('sandbox'),'allow-scripts allow-forms allow-modals');
assert.equal(document.querySelector('iframe').getAttribute('referrerpolicy'),'no-referrer');
assert.equal(document.querySelector('iframe').getAttribute('srcdoc'),framed(hostile));
assert.deepEqual(document.querySelector('meta').content.split(';').map(s=>s.trim()),["default-src 'none'","script-src 'unsafe-inline'","connect-src 'none'",'img-src data:',"style-src 'unsafe-inline'","font-src 'none'","media-src 'none'","frame-src 'none'","object-src 'none'","base-uri 'none'","form-action 'none'"]);
const page=`<!doctype html><h1>GL2 browser sandbox check</h1><button id="button" onclick="this.textContent='PASS: click handler ran'">Try inline click</button><pre id="result">Starting</pre><script>
const lines=['PASS: inline script ran'];
const add=s=>{lines.push(s);document.getElementById('result').textContent=lines.join('\\n');};
try{parent.document.body;add('FAIL: parent was readable');}catch(e){add('PASS: parent access blocked');}
try{localStorage.setItem('gl2','x');add(localStorage.getItem('gl2')==='x'?'PASS: memory storage works':'FAIL: storage');}catch(e){add('FAIL: memory storage unavailable');}
try{eval('1');add('FAIL: eval allowed');}catch(e){add('PASS: inherited CSP blocked eval');}
document.addEventListener('securitypolicyviolation',e=>add('PASS: inherited CSP blocked '+e.effectiveDirective));
fetch('https://example.invalid/gl2').then(()=>add('FAIL: fetch allowed'),()=>{});
const script=document.createElement('script');script.src='https://example.invalid/gl2.js';document.body.append(script);
const frame=document.createElement('iframe');frame.src='https://example.invalid/gl2';document.body.append(frame);
</script>`;
if(process.argv.includes('--write-browser-fixture')){const file=path.join(__dirname,'gl2-browser-preview.html');fs.writeFileSync(file,wrapper(page));console.log(file);}
console.log('GL2 external preview: PASS (exact Rust wrapper template, one scriptless outer document, sandbox/referrer/CSP directives, hostile srcdoc round trip)');
