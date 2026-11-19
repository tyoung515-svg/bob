// TypeScript source loader for the existing dependency-free DOM harnesses.
const fs=require('node:fs'),ts=require('typescript');
for(const ext of ['.tsx','.ts']) require.extensions[ext]=(mod,file)=>mod._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2021,esModuleInterop:true}}).outputText,file);
require.extensions['.css']=()=>{};
