// Pure turn state for the portal chat surface (CONTRACT §3, §5.5).
//
// No React, no DOM, no clock: PortalApp drives this reducer and
// harness/portal-turn.cjs replays the same events against it, so "every
// terminal outcome releases the in-flight lock" is a tested property instead of
// a comment. A turn may end with `complete`, `stopped`, `error` or a transport
// fault; all four clear `stream` and `pending`, keep any partial assistant text
// as a local (never server-attributed) note — except a `complete` turn whose
// readback already put that text in the transcript from a server row (R2-1) —
// and stage nothing for a resend.

export type ChatPin = { backend: string; model: string };

export type StreamState = {
  request_id: string;
  conversation_id: string;
  content: string;
  model: string | null;
  backend: string | null;
  /** The pin this window asked for; display only, the server echo is `pins`. */
  pin: ChatPin | null;
  done: boolean;
  stopped: string | null;
  error: string | null;
};

export type PendingState = { conversation_id: string; text: string; request_id: string | null; pin: ChatPin | null };

export type TerminalKind = 'complete' | 'stopped' | 'error' | 'transport';

export type TerminalNote = {
  stopped_reason?: string | null;
  step_extensions?: number;
  unanswered_question?: boolean;
  kind: TerminalKind;
  message_id?: string | null;
  conversation_id: string;
  request_id: string;
  code: string | null;
  detail: string | null;
  /** Assistant text that arrived before the turn ended. Kept, never re-sent. */
  partial: string;
  /**
   * True when the post-`complete` readback put this turn's text in the
   * transcript from a server row (a stored message or the server's
   * `message_id`). The local copy is then cleared so the answer is never
   * rendered twice (R2-1).
   */
  in_transcript: boolean;
  /** Honest one-line transcript status; never claims more than the server said. */
  line: string;
};

export type TurnState = {
  stream: StreamState | null;
  pending: PendingState | null;
  terminal: TerminalNote | null;
};

export type TurnEvent =
  | { type: 'observe'; conversation_id: string; request_id: string }
  | { type: 'send'; conversation_id: string; text: string; pin: ChatPin | null }
  | { type: 'accepted'; request_id: string }
  | { type: 'chunk'; request_id: string; content: string; model: string | null; backend: string | null }
  | { type: 'refused' }
  | {
      type: 'terminal';
      request_id: string;
      kind: 'complete' | 'stopped' | 'error';
      message_id?: string | null;
      code: string | null;
      detail: string | null;
      by_user?: boolean;
      unanswered_question?: boolean;
      stopped_reason?: string | null;
      step_extensions?: number;
    }
  | { type: 'transport_fault'; code: string; detail: string; request_id?: string | null; conversation_id?: string; line?: string }
  /**
   * The reconcile read the server history after `complete` and the turn's text
   * is now in the transcript from a server row. Render it once, from there.
   */
  | { type: 'turn_in_transcript'; message_id?: string | null }
  | { type: 'note'; line: string }
  | { type: 'reset' };

export const STOPPED_BY_USER_LINE = 'stopped by you — partial output kept';
export const STOPPED_REMOTE_LINE =
  'the turn ended without a complete frame — partial output may be kept; outcome unknown, inspect history';
export const TRANSPORT_LINE = 'stream ended unexpectedly — outcome unknown, inspect history';
export const SEND_TRANSPORT_LINE = 'the send did not return — outcome unknown, inspect history';
export const COMPLETE_LINE = 'answered — the server reported complete; history re-read from the server';
export const COMPLETE_UNREAD_LINE = 'answered — the server reported complete, but the history readback failed';

export function errorLine(code: string | null): string {
  return `stream ended with an error (${code ?? 'no code reported'}) — nothing was resent; inspect history for the server record`;
}

export const INITIAL_TURN: TurnState = { stream: null, pending: null, terminal: null };

function noteFrom(
  kind: TerminalKind,
  conversation_id: string,
  request_id: string,
  code: string | null,
  detail: string | null,
  partial: string,
  line: string,
): TerminalNote {
  return { kind, conversation_id, request_id, code, detail, partial, in_transcript: false, line };
}

export function turnReducer(state: TurnState, event: TurnEvent): TurnState {
  switch (event.type) {
    case 'observe':
      if (state.stream?.request_id === event.request_id || state.terminal?.request_id === event.request_id) return state;
      return { pending: null, terminal: null, stream: { request_id:event.request_id, conversation_id:event.conversation_id, content:'',model:null,backend:null,pin:null,done:false,stopped:null,error:null } };
    case 'send':
      return {
        stream: null,
        pending: { conversation_id: event.conversation_id, text: event.text, request_id: null, pin: event.pin },
        terminal: null,
      };
    case 'accepted': {
      const pending = state.pending;
      if (!pending) return state;
      return {
        stream: {
          request_id: event.request_id,
          conversation_id: pending.conversation_id,
          content: '',
          model: pending.pin?.model ?? null,
          backend: pending.pin?.backend ?? null,
          pin: pending.pin,
          done: false,
          stopped: null,
          error: null,
        },
        pending: { ...pending, request_id: event.request_id },
        terminal: null,
      };
    }
    case 'chunk': {
      const stream = state.stream;
      if (!stream || stream.done || stream.request_id !== event.request_id) return state;
      return {
        ...state,
        stream: {
          ...stream,
          content: stream.content + event.content,
          model: event.model ?? stream.model,
          backend: event.backend ?? stream.backend,
        },
      };
    }
    case 'refused':
      return { ...state, stream: null, pending: null };
    case 'terminal': {
      const stream = state.stream;
      const conversation_id = stream?.conversation_id ?? state.pending?.conversation_id ?? state.terminal?.conversation_id ?? '';
      const request_id = stream?.request_id ?? state.pending?.request_id ?? event.request_id;
      if (!stream) {
        if (state.pending) return { stream: null, pending: null, terminal: noteFrom(event.kind, conversation_id, request_id, event.code, event.detail, '', event.kind === 'stopped' ? STOPPED_REMOTE_LINE : errorLine(event.code)) };
        // A second terminal frame (the broker sends `error` then `stopped`) must
        // not duplicate the note or re-open a lock.
        if (state.terminal && state.terminal.request_id === event.request_id) {
          return { ...state, terminal: { ...state.terminal, code: state.terminal.code ?? event.code } };
        }
        return {
          ...state,
          terminal: noteFrom(
            event.kind,
            conversation_id,
            request_id,
            event.code,
            event.detail,
            '',
            event.kind === 'stopped' ? STOPPED_REMOTE_LINE : errorLine(event.code),
          ),
        };
      }
      const line =
        event.kind === 'complete'
          ? COMPLETE_LINE
          : event.kind === 'stopped'
            ? event.by_user
              ? STOPPED_BY_USER_LINE
              : STOPPED_REMOTE_LINE
            : errorLine(event.code);
      return {
        stream: null,
        pending: null,
        terminal: { ...noteFrom(event.kind, conversation_id, request_id, event.code, event.detail, stream.content, line), message_id: event.message_id,
          stopped_reason: event.stopped_reason, step_extensions: event.step_extensions,
          ...(event.unanswered_question ? { unanswered_question: true } : {}) },
      };
    }
    case 'transport_fault': {
      const stream = state.stream;
      const request_id = stream?.request_id ?? event.request_id ?? '';
      const conversation_id = stream?.conversation_id ?? event.conversation_id ?? state.terminal?.conversation_id ?? '';
      const partial = stream?.content ?? '';
      return {
        stream: null,
        pending: null,
        terminal: noteFrom(
          'transport',
          conversation_id,
          request_id,
          event.code,
          event.detail,
          partial,
          event.line ?? (stream ? TRANSPORT_LINE : SEND_TRANSPORT_LINE),
        ),
      };
    }
    case 'turn_in_transcript': {
      // The answer is on screen from the server's own row, so drop the local
      // copy: rendering it again under a "local" provenance line was R2-1.
      const terminal = state.terminal;
      if (!terminal || terminal.in_transcript) return state;
      return { ...state, terminal: { ...terminal, message_id: event.message_id ?? terminal.message_id, in_transcript: true, partial: '' } };
    }
    case 'note': {
      if (!state.terminal) return state;
      return { ...state, terminal: { ...state.terminal, line: event.line } };
    }
    case 'reset':
      return INITIAL_TURN;
    default:
      return state;
  }
}

/**
 * The provenance line under a rendered terminal note (R2-1). It must be true in
 * every case it is shown: a `complete` turn's streamed text is only rendered
 * when the readback did not confirm it, and every other outcome leaves a local
 * copy this window never confirmed as a stored message.
 */
export function terminalProvenance(note: TerminalNote): string {
  const head = `local:turn:${note.request_id || '(no request id)'} — conversation ${note.conversation_id || 'unknown'}`;
  if (note.kind === 'complete') {
    return `${head}; the server reported complete but the history readback did not confirm this text as a stored message — it is the streamed copy, kept locally, and was not resent`;
  }
  const what =
    note.kind === 'transport' ? 'the transport faulted' : note.kind === 'stopped' ? 'the server reported the turn stopped' : 'the server reported an error';
  return `${head}; ${what}, so this is the streamed copy kept locally — this window has not confirmed it in server history and did not resend it`;
}

/** A stream is live only while it is unfinished and belongs to the open conversation. */
export function liveStream(state: TurnState, conversationId: string | null | undefined): StreamState | null {
  const stream = state.stream;
  if (!stream || stream.done) return null;
  if (!conversationId || stream.conversation_id !== conversationId) return null;
  return stream;
}

/** The in-flight lock: a send, a stop, an approval decision or a streaming turn. */
export function inFlight(state: TurnState): boolean {
  const stream = state.stream;
  return !!stream && !stream.done;
}

export type LockFlags = {
  sending: boolean;
  stopping: boolean;
  approvalBusy: boolean;
  inFlight: boolean;
  /** Deliberate, separate gate: unknown send outcome (CONTRACT §5.5, no resend). */
  unknownOutcome: boolean;
};

/**
 * Send is blocked while something is in flight or while the outcome of an
 * earlier send is unknown. After any terminal event `inFlight` is false, so only
 * the honest unknown-outcome gate can still hold the composer — and that gate is
 * released by reading the server history, never by a refresh.
 */
export function composerLocked(flags: LockFlags): boolean {
  return flags.sending || flags.stopping || flags.approvalBusy || flags.inFlight || flags.unknownOutcome;
}

/** A live conversation does not lock navigation to other conversations. */
export function navigationLocked(flags: { busy: boolean; inFlight: boolean }): boolean {
  return flags.busy;
}

// --------------------------------------------------------- unknown send gate

/** The minimum a history row must report for the release decision. */
export type HistoryRow = { id: string; role: string; content: string };

/**
 * Snapshot of the transcript at the moment a send left this window (R2-7).
 * The pre-send length plus the id of its last row anchor the search window, so
 * an older identical message can never stand in for the send in doubt. `text`
 * is the prompt the send carried: with the anchor it names the **turn**, which
 * is what a completed answer is matched to (R3-1). There is no timestamp here on
 * purpose — see `turnAnswerRowId` (R3-2).
 */
export type SendMark = { conversation_id: string; count: number; last_id: string | null; text: string; draft?: string };

export type SendInspection =
  | { state: 'answered'; message_id: string }
  | { state: 'stored_unanswered'; message_id: string }
  | { state: 'not_stored' }
  | { state: 'unknown'; reason: string };

/**
 * Decide an unknown send's outcome from a fresh history read, looking only at
 * rows that appear after the pre-send mark. Text equal to the unconfirmed text
 * that sits before the mark belongs to an earlier send and proves nothing.
 * When the mark cannot be anchored in the readback, the gate is kept: a wrong
 * release would let the owner send the same text twice.
 */
export function inspectUnknownSend(
  rows: HistoryRow[],
  mark: SendMark | null | undefined,
  text: string,
  conversation_id: string,
): SendInspection {
  if (!mark || mark.conversation_id !== conversation_id) {
    return { state: 'unknown', reason: 'no send marker for this conversation' };
  }
  let start: number;
  if (mark.last_id) {
    const at = rows.findIndex((row) => row.id === mark.last_id);
    if (at < 0) return { state: 'unknown', reason: 'the last pre-send message is not in this readback' };
    start = at + 1;
  } else if (rows.length < mark.count) {
    return { state: 'unknown', reason: 'the readback is shorter than the pre-send transcript' };
  } else {
    start = mark.count;
  }
  const at = rows.findIndex((row, index) => index >= start && row.role === 'user' && row.content === text);
  if (at < 0) return { state: 'not_stored' };
  const answered = rows.slice(at + 1).some((row) => row.role === 'assistant');
  return answered ? { state: 'answered', message_id: rows[at].id } : { state: 'stored_unanswered', message_id: rows[at].id };
}

// ----------------------------------- turn answer matching (R4-1)

/**
 * Answer text reduced to what "the same answer" means across two sources: the
 * streamed chunks and the stored row. The broker never promised them
 * byte-identical (line endings and trailing blank lines differ), so line
 * endings are normalized, each line is trimmed, and outer blank lines dropped.
 */
export function normalizeAnswerText(text: string): string {
  const lines = text.replace(/\r\n?/g, '\n').split('\n').map((line) => line.trim());
  while (lines.length > 0 && lines[0] === '') lines.shift();
  while (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return lines.join('\n');
}

/** A history row as this decision reads it: identity, role and text. */
export type AnswerCandidate = HistoryRow & { metadata?: unknown };

/**
 * The history row that holds THIS turn's answer, or null when it is not
 * readable yet (R4-1, R3-1).
 *
 * The completion frame's `message_id` is NOT the stored row's id — observed in
 * the real app as completion `0660b76f…` while the stored row was `9b221ff8…`
 * with the same text — so an id-only check leaves the chunk-derived copy on
 * screen beside the stored row. Match the turn instead:
 *
 * 1. the rows this send can have produced are those **after** the pre-send mark;
 * 2. this turn's own prompt row is the first user row in that window whose text
 *    equals the text that was sent (`mark.text`) — never "the last row that was
 *    on screen", which is the previous turn's prompt when that turn's readback
 *    landed late and its question had the same text (R3-1);
 * 3. the answer is the first assistant row after that prompt whose text equals
 *    the streamed text after normalization, and it must come **before the next
 *    dispatch**: a correlated operator answer resumes the same turn, while a
 *    later prompt ends the window. An answer missing from the readback cannot
 *    be borrowed from a later turn.
 *
 * Row `created_at` is deliberately not consulted: it is the server's clock and
 * the mark is the client's, so a clock step (sleep/resume, NTP) would decide
 * whether an answer is recognised (R3-2). When any anchor is missing this
 * returns null and the caller keeps the honest "not yet readable" article — the
 * same back-off `inspectUnknownSend()` uses for the unknown-outcome gate.
 */
export function turnAnswerRowId(
  rows: AnswerCandidate[],
  mark: SendMark | null | undefined,
  text: string,
  conversation_id: string,
): string | null {
  const wanted = normalizeAnswerText(text);
  if (!wanted) return null;
  if (!mark || mark.conversation_id !== conversation_id) return null;
  const prompt = normalizeAnswerText(mark.text || '');
  if (!prompt) return null;
  // The window this send can have written. A mark that cannot be found in the
  // readback proves nothing, and neither does a page shorter than the pre-send
  // transcript.
  let from: number;
  if (mark.last_id) {
    const at = rows.findIndex((row) => row.id === mark.last_id);
    if (at < 0) return null;
    from = at + 1;
  } else if (rows.length >= mark.count) {
    from = mark.count;
  } else {
    return null;
  }
  const promptAt = rows.findIndex(
    (row, index) => index >= from && row.role === 'user' && normalizeAnswerText(row.content) === prompt,
  );
  if (promptAt < 0) return null;
  const parts: string[] = [];
  let questionId: unknown = null;
  for (let index = promptAt + 1; index < rows.length; index += 1) {
    const row = rows[index];
    const meta = row.metadata && typeof row.metadata === 'object' ? row.metadata as Record<string, unknown> : {};
    if (meta.history_readback === 'pending') continue;
    // An operator answer resumes this same turn; it is not a new dispatch.
    // Newer servers identify it by kind/question_id; keep the old wire form.
    if (row.role === 'user') {
      const answer = meta.kind === 'operator_answer'
        ? !!questionId && meta.question_id === questionId
        : !meta.kind && row.content.startsWith('Operator answer to: ');
      if (!answer) return null;
      questionId = null;
    }
    if (row.role === 'assistant') {
      parts.push(row.content);
      if (meta.kind === 'pre_question') { questionId = meta.question_id; continue; }
      if (normalizeAnswerText(row.content) === wanted || normalizeAnswerText(parts.join('\n')) === wanted || normalizeAnswerText(parts.join('')) === wanted) return row.id;
    }
  }
  return null;
}

// ------------------------------------------------------------------ chat pins

/**
 * The broker applies a model pin only together with its backend
 * (`invalid_pin` otherwise), so a pin is always the pair or nothing.
 */
export function pickFromRow(row: { backend?: string | null; model?: string | null; id?: string | null }): ChatPin | null {
  const backend = (row.backend ?? '').trim();
  const model = (row.model ?? '').trim();
  if (!backend || !model) return null;
  return { backend, model };
}

/**
 * Why a catalog row cannot be pinned, or null when it can. A row without a
 * model string is refused rather than sent as `model=<backend>`: the server
 * never named that model, so dispatching it would be an invented pin (R2-8).
 */
export function pinRefusal(row: { backend?: string | null; model?: string | null; id?: string | null }): string | null {
  if (!(row.backend ?? '').trim()) return 'no backend reported';
  if (!(row.model ?? '').trim()) return 'the server reported no model string';
  return null;
}

/** Rebuild the picker's selection from the pins the server reported for a conversation. */
export function pickFromServer(model: string | null | undefined, backend: string | null | undefined): ChatPin | null {
  const pair = { backend: (backend ?? '').trim(), model: (model ?? '').trim() };
  if (!pair.model) return null;
  if (!pair.backend) return null;
  return pair;
}

export function modelKey(pin: ChatPin): string {
  return `${pin.backend}\u001f${pin.model}`;
}

export function pinLabel(row: { backend?: string | null; model?: string | null; id?: string | null; label?: string | null }): string {
  const backend = (row.backend ?? '').trim() || 'no backend reported';
  const model = (row.model ?? '').trim() || (backend === 'codex_code' || backend === 'openai_codex' ? 'Codex default' : 'Account default');
  return `${row.label ? `${row.label} · ` : ''}${model} · ${backend}`;
}

/** Send both halves of the pin, or neither. */
export function sendPinArgs(pin: ChatPin | null): { backend?: string; model?: string } {
  if (!pin || !pin.backend || !pin.model) return {};
  return { backend: pin.backend, model: pin.model };
}

export function pinLine(pin: ChatPin | null): string {
  return pin ? `model ${pin.model} · backend ${pin.backend}` : 'server default';
}
