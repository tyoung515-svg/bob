// Explicit, candidate-only normalization. Never run on an owner checkout.
const cp=require('node:child_process'),fs=require('node:fs'),assert=require('node:assert/strict');
assert.equal(cp.execFileSync('git',['branch','--show-current'],{encoding:'utf8'}).trim(),'sprint/windows-ui-20261024');
const files=cp.execFileSync('git',['ls-files','-z','engine/tests/fixtures']).toString('utf8').split('\0').filter(p=>/\.(json|jsonl)$/.test(p));
cp.execFileSync('git',['diff','--exit-code','HEAD','--',...files]);
for(const file of files){const canonical=cp.execFileSync('git',['show','HEAD:'+file]),actual=fs.readFileSync(file);assert(Buffer.from(actual.toString('utf8'),'utf8').equals(actual),'fixture is not UTF-8');assert(Buffer.from(actual.toString('utf8').replace(/\r\n/g,'\n'),'utf8').equals(canonical),'refuse to discard a non-EOL fixture change');fs.writeFileSync(file,canonical);}
console.log('Re-materialized verified unchanged checksum Git fixture bytes as LF:',files.length);
