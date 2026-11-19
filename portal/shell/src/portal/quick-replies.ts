/**
 * Turn the choices Bob offers into buttons.
 *
 * Two sources, in order of trust:
 *  1. an explicit block — ```options … ``` or :::options … ::: — one choice per line;
 *  2. a list introduced by a choice cue ("Options", "Next step", "Pick one",
 *     "Want me to…", "Say 1 or 2…"), which is where the real traffic is: a model
 *     writes "**Next step — pick one:**" and then a numbered list, often with a
 *     blank line between items.
 *
 * (2) stays conservative on purpose: a list only becomes buttons when a cue line
 * sits above it, because a fan-out answer is full of bullet lists that are
 * content, not choices. Nothing here sends anything by itself — the app puts the
 * choice in the composer so the operator still presses Send.
 */

const CUE = /(option|choose|pick one|pick a|next step|would you like|want me to|shall i|which (?:one|should)|your call|say\s+"?\d)/i;
const MAX_OPTIONS = 6;
const MAX_LEN = 600;

function clean(line: string): string {
  return line
    .replace(/^\s*(?:[-*+]|\d+[.)]|\[[ xX]\])\s+/, '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .trim();
}

function explicit(text: string): string[] | null {
  const fence = text.match(/```options?\s*\n([\s\S]*?)```/i);
  const colon = text.match(/:::options\s*\n([\s\S]*?)\n:::/i);
  const body = fence?.[1] ?? colon?.[1];
  if (!body) return null;
  const items = body
    .split('\n')
    .map(clean)
    .filter((item) => item.length > 0 && item.length <= MAX_LEN);
  return items.length >= 2 ? items.slice(0, MAX_OPTIONS) : null;
}

const ITEM = /^\s*(?:\d+[.)]|[-*+])\s+\S/;

function fromList(text: string): string[] | null {
  const lines = text.split('\n');

  // Walk back from the end looking for a cue line. The LAST matching line is not
  // always the right one — a trailing "Say 1 or 2 and I'll run it" is itself a
  // cue with no list under it — so each candidate is tried and we keep the first
  // one that actually introduces a list. Blank lines between items are allowed;
  // the run ends at the first non-blank, non-item line.
  for (let cueAt = lines.length - 1; cueAt >= 0; cueAt -= 1) {
    const line = lines[cueAt].trim();
    if (!line || ITEM.test(line) || !CUE.test(line)) continue;

    let i = cueAt + 1;
    while (i < lines.length && !lines[i].trim()) i += 1;
    if (i >= lines.length || !ITEM.test(lines[i])) continue;
    if (i - cueAt > 6) continue; // the cue must introduce the list, not precede prose

    const items: string[] = [];
    while (i < lines.length) {
      if (!lines[i].trim()) {
        i += 1;
        continue;
      }
      if (!ITEM.test(lines[i])) break;
      items.push(clean(lines[i]));
      i += 1;
    }
    const usable = items.filter((item) => item.length > 0 && item.length <= MAX_LEN);
    if (usable.length >= 2 && usable.length <= MAX_OPTIONS) return usable;
  }
  return null;
}

export function extractOptions(text: string): string[] {
  return explicit(text) ?? fromList(text) ?? [];
}

/** Remove the option block from the text handed to the renderer, so a choice is
 *  shown once — as a button — and not again as a code block. Prose lists stay in
 *  the transcript: the reader loses nothing if the buttons disappear. */
export function stripOptionBlock(text: string): string {
  return text
    .replace(/```options?\s*\n[\s\S]*?```/gi, '')
    .replace(/:::options\s*\n[\s\S]*?\n:::/gi, '')
    .trimEnd();
}
