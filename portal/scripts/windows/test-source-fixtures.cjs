// Byte-level release fixture test: works before the Linux-only engine compiles.
const fs=require('node:fs'),cp=require('node:child_process'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const files=cp.execFileSync('git',['ls-files','-z','engine/tests/fixtures']).toString('utf8').split('\0').filter(p=>/\.(json|jsonl)$/.test(p));
assert(files.length>0,'no fixture bytes were checked');const rows=[];
for(const file of files){const expected=cp.execFileSync('git',['show','HEAD:'+file]);const actual=fs.readFileSync(file);const hash=b=>crypto.createHash('sha256').update(b).digest('hex');rows.push({file,expected_sha256:hash(expected),actual_sha256:hash(actual),matched:actual.equals(expected)});}
console.log(JSON.stringify({count:rows.length,rows},null,2));assert(rows.every(r=>r.matched),'Windows checkout changed checksum-pinned fixture bytes (CRLF conversion is not a source change)');
