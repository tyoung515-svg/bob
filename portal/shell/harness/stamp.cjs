"use strict";
// tsc cannot emit a package.json into outDir, and shell/package.json declares
// "type": "module" — so the CommonJS output in .harness-build/ needs a local
// override or node treats it as ESM ("exports is not defined").
const fs = require("node:fs");
const path = require("node:path");

const dir = path.join(__dirname, "..", ".harness-build");
fs.mkdirSync(dir, { recursive: true });
fs.writeFileSync(path.join(dir, "package.json"), JSON.stringify({ type: "commonjs" }) + "\n");
