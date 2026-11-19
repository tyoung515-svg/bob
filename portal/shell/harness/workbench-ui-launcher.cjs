const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),{spawnSync}=require('node:child_process');
const temp=fs.mkdtempSync(path.join(process.env.XDG_CACHE_HOME||path.join(os.homedir(),'.cache'),'workbench-ui-launcher-'));
const script=path.join(temp,'scripts/linux/run-functional.sh'),binary=path.join(temp,'shell/src-tauri/target/debug/bob-slice-shell');fs.mkdirSync(path.dirname(script),{recursive:true});fs.mkdirSync(path.dirname(binary),{recursive:true});fs.copyFileSync('../scripts/linux/run-functional.sh',script);
fs.writeFileSync(binary,'#!/usr/bin/env bash\nprintf "ROOT=%s\\nNV=%s\\nWEBKIT=%s\\nFIXTURE=%s\\n" "$BOB_FUNCTIONAL_DATA_ROOT" "${__NV_DISABLE_EXPLICIT_SYNC-unset}" "${WEBKIT_DISABLE_DMABUF_RENDERER-unset}" "${BOB_DEVELOPER_FIXTURE-unset}"\n');fs.chmodSync(binary,0o700);
const base={...process.env,DISPLAY:':private-test',XDG_DATA_HOME:path.join(temp,'data'),XDG_RUNTIME_DIR:temp,BOB_DEVELOPER_FIXTURE:'bad'};for(const k of ['BOB_FUNCTIONAL_DATA_ROOT','__NV_DISABLE_EXPLICIT_SYNC','WEBKIT_DISABLE_DMABUF_RENDERER'])delete base[k];
const run=(args=[],env={})=>spawnSync('bash',[script,'--no-build',...args],{env:{...base,...env},encoding:'utf8'});
try{
 let r=run();assert.equal(r.status,0,r.stderr);assert.ok(r.stdout.includes(`ROOT=${temp}/data/bob-workbench-p1`),'private P1 default never owner bob-functional-v2');assert.ok(r.stdout.includes('NV=1\nWEBKIT=1'),'safe defaults scoped to application');assert.ok(r.stdout.includes('FIXTURE=unset'));
 r=run([],{BOB_FUNCTIONAL_DATA_ROOT:'/explicit/store',__NV_DISABLE_EXPLICIT_SYNC:'0',WEBKIT_DISABLE_DMABUF_RENDERER:''});assert.ok(r.stdout.includes('ROOT=/explicit/store'));assert.ok(r.stdout.includes('NV=0\nWEBKIT=\n'),'preserve explicit including empty override');
 r=run(['--data-root','/flag/store']);assert.ok(r.stdout.includes('ROOT=/flag/store'));
 r=run(['--rendering','native']);assert.equal(r.status,0,r.stderr);assert.ok(r.stdout.includes('NV=unset\nWEBKIT=unset'));
 r=run(['--rendering','bogus']);assert.equal(r.status,64);
 r=run(['--check']);assert.equal(r.status,0);assert.ok(r.stdout.includes('bob-workbench-p1'));assert.ok(r.stdout.includes('Rendering: safe'));
 r=run(['--help']);assert.ok(r.stdout.includes('--rendering'));
 r=run([],{DISPLAY:'',WAYLAND_DISPLAY:''});assert.equal(r.status,6);
 console.log('workbench launcher private storage/safe env/overrides/native/check/negative controls PASS (disposable binary, no desktop)');
}finally{fs.rmSync(temp,{recursive:true,force:true})}
