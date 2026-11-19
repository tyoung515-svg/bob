// Render helpers for values that reach the DOM. These are NOT wire types:
// every wire shape in this shell comes from `protocol.ts`, which is generated
// from the engine's normative schema (`engine/src/wire_schema.rs`) and must
// never be edited by hand (A-10 / EXEC-49).

/** Render any preview/wire value as inert text (React escapes it; this
 * helper only normalizes non-strings). Never returns markup. */
export function asText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}
