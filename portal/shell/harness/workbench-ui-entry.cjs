require('./workbench-ui-register.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs');
const main=fs.readFileSync('src/main.tsx','utf8');assert.match(main,/WorkbenchApp/,'normal product entry must render new live workbench');assert.match(main,/workbench\.css/);
const app=fs.readFileSync('src/WorkbenchApp.tsx','utf8');assert.ok(!/fixture|mockDashboard|setTimeout.*completed/i.test(app),'no production synthetic fixture/progress');
const css=fs.readFileSync('src/workbench.css','utf8');assert.match(css,/minmax\(0,\s*1fr\)/);assert.match(css,/:focus-visible/);assert.match(css,/@media[^{}]*900px/);assert.ok(!/https?:|@import/.test(css),'local-only visual assets');
console.log('workbench default entry/live-only source/responsive CSS guard PASS');
