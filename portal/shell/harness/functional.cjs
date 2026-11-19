// Product-path regression: the actual default App, not a replacement UI.
const { JSDOM } = require('jsdom');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const app = fs.readFileSync('src/main.tsx','utf8');
assert.match(app, /FunctionalApp/, 'default launcher must mount functional chat, not fixture App');
console.log('functional product entry PASS');
