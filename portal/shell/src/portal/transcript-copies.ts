import type { Message } from './portal-protocol';
import { normalizeAnswerText, turnAnswerRowId, type SendMark } from './portal-state';

/** Identity first; anchored text matching covers servers with a different completion id. */
export function hasTurnRow(rows: Message[], conversation: string, request: string, messageId: string | null | undefined, text: string, mark: SendMark | null): boolean {
  return turnRowId(rows, conversation, request, messageId, text, mark) !== null;
}

/** Locate the same final row for live completion details and copy suppression. */
export function turnRowId(rows: Message[], conversation: string, request: string, messageId: string | null | undefined, text: string, mark: SendMark | null): string | null {
  const identified = rows.find(row => row.role === 'assistant' && row.id === messageId);
  if (identified) return identified.id;
  const parts = request ? rows.filter(row => row.role === 'assistant' && metadata(row).request_id === request) : [];
  const wanted = normalizeAnswerText(text);
  // A request id may be on a question prefix too. Require a final segment and
  // the streamed text (one row or the joined parts), even without a send mark.
  if (parts.length && metadata(parts[parts.length - 1]).kind !== 'pre_question' &&
    (!wanted || parts.some(row => metadata(row).kind !== 'pre_question' && normalizeAnswerText(row.content) === wanted) ||
      normalizeAnswerText(parts.map(row => row.content).join('\n')) === wanted ||
      normalizeAnswerText(parts.map(row => row.content).join('')) === wanted)) return parts[parts.length - 1].id;
  return turnAnswerRowId(rows, mark, text, conversation);
}

const metadata = (row: Message): Record<string, unknown> => row.metadata && typeof row.metadata === 'object' ? row.metadata as Record<string, unknown> : {};
export const pendingReadbackRows = (rows: Message[]) => rows.some(row => metadata(row).history_readback === 'pending');

/** Collapse a completion copy and its stored Builder final within one turn.
 * Never collapse identical answers across a new user dispatch. */
export function singleRenderRows(rows: Message[]): Message[] {
  const result: Message[] = [];
  for (const row of rows) {
    const previous = result[result.length - 1];
    if (previous?.role === 'assistant' && row.role === 'assistant' && normalizeAnswerText(previous.content) === normalizeAnswerText(row.content) &&
      (metadata(previous).history_readback === 'pending' || metadata(row).history_readback === 'pending' ||
        !!metadata(row).request_id && metadata(previous).request_id === metadata(row).request_id || row.content.startsWith('Builder tool results:'))) {
      if (metadata(row).history_readback !== 'pending') result[result.length - 1] = row;
    } else result.push(row);
  }
  return result;
}
