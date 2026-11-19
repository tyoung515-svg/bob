// Quick-reply extraction: the choices Bob offers must become buttons, and a
// content bullet list must not. Driven against the real compiled module.
const assert = require('node:assert');
const { extractOptions, stripOptionBlock } = require('../src/portal/quick-replies.ts');

// Shape taken from a real 2026-10-27 reply: a bold cue line, a blank line, then a
// numbered list whose items are long sentences, closed by a prose "Say 1 or 2".
const real = [
  'Fetch succeeded (HTTP 200, 1,825 bytes, not truncated).',
  '',
  '**Next step — pick one:**',
  '',
  '1. **Widen and dedupe the firehose.** Re-pull with limit=100 and dedupe.',
  '',
  '2. **Resolve one of these to its origin.** Chase one candidate upstream.',
  '',
  'Say "1" or "2" and I\'ll run it.',
].join('\n');
const fromReal = extractOptions(real);
assert.equal(fromReal.length, 2, `expected two choices: ${JSON.stringify(fromReal)}`);
assert.ok(fromReal[0].startsWith('Widen and dedupe'), fromReal[0]);
assert.ok(fromReal[1].startsWith('Resolve one of these'), fromReal[1]);

// A content bullet list with no cue must NOT become buttons.
const content = 'Notes:\n\n- Meta released a model\n- Mistral released a model';
assert.deepEqual(extractOptions(content), []);

// An explicit block wins.
assert.deepEqual(extractOptions('Here.\n\nOptions:\n- Show me the pricing page\n- Draft the reply to Dana'),
  ['Show me the pricing page', 'Draft the reply to Dana']);

// A fenced options block is extracted and stripped from the rendered text.
const fenced = 'Done.\n```options\nShip it\nWait a day\n```';
assert.deepEqual(extractOptions(fenced), ['Ship it', 'Wait a day']);
assert.equal(stripOptionBlock(fenced), 'Done.');

// A single-item list is not a choice.
assert.deepEqual(extractOptions('Options:\n- only one'), []);

console.log('portal quick replies: PASS (cue lists become buttons, content bullets do not, explicit blocks win and are stripped)');
