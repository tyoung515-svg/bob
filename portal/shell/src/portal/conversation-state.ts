import { useReducer, useRef } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { Message, Pins, PortalError, WorkspaceReceipt } from './portal-protocol';
import { INITIAL_TURN, type ChatPin, type SendMark, type TurnState } from './portal-state';

export type Question = { question_id: string; question: string; choices: string[]; origin?: string; created_at?: string };
export type ChatFault = PortalError & { sent: boolean; request_id?: string; text?: string; conversation_id?: string };
export type ToolActivity = { request_id: string; phase: string; tool: string; args?: string; preview?: string; ok?: boolean; elapsed_ms?: number; observed_ms?: number };

function initial() {
  return {
    messages: [] as Message[], historyVersion: 0, hasMore: false, pins: null as Pins | null,
    // Completion details observed in this window, keyed only after row matching.
    replyOutcomes: {} as Record<string, { stopped_reason?: string | null; step_extensions?: number }>,
    face: '', pin: null as ChatPin | null, sentPin: null as ChatPin | null, research: false,
    input: '', turn: INITIAL_TURN, sending: false, stopping: false, stopCleared: null as string | null,
    approvalBusy: false, fault: null as ChatFault | null, error: null as PortalError | null,
    notice: '', activity: [] as ToolActivity[], question: null as Question | null,
    questions: [] as Question[], questionDrafts: {} as Record<string, string>, answering: false,
    workspaceReceipt: null as WorkspaceReceipt | null, finishedUnseen: false,
    builderWaiting: false, builderApprovals: [] as string[], approvalFrames: {} as Record<string, string>,
    sendMark: null as SendMark | null, stopRequested: null as string | null, readbackRetry: null as number | null,
  };
}
export type ConversationState = ReturnType<typeof initial>;
type Setters = { [K in keyof ConversationState as `set${Capitalize<K>}`]: Dispatch<SetStateAction<ConversationState[K]>> };
type Refs = { turnRef: { current: TurnState }; questionRef: { current: Question | null }; sendMark: { current: SendMark | null }; stopRequested: { current: string | null }; readbackRetry: { current: number | null } };
type Scope = Omit<ConversationState, 'sendMark' | 'stopRequested' | 'readbackRetry'> & Setters & Refs;

/** Synchronous writes let frames and async replies use the same conversation
 * state before React commits. A bound setter never follows later navigation. */
export function useConversationStates() {
  const [, render] = useReducer((n: number) => n + 1, 0);
  const ref = useRef<{
    records: Map<string, ConversationState>; requests: Map<string, string>;
    scope: (id: string) => Scope;
  }>();
  if (!ref.current) {
    const records = new Map<string, ConversationState>(), requests = new Map<string, string>();
    const scopes = new Map<string, Scope>();
    const scope = (id: string): Scope => {
      const cached = scopes.get(id);
      if (cached) return cached;
      const state = initial(); records.set(id, state);
      const result = {} as Scope;
      for (const key of Object.keys(state) as (keyof ConversationState)[]) {
        Object.defineProperty(result, key, { configurable: true, enumerable: true, get: () => state[key] });
        Object.defineProperty(result, `set${key[0].toUpperCase()}${key.slice(1)}`, { enumerable: true, value: (next: unknown) => {
          const value = typeof next === 'function' ? next(state[key]) : next;
          if (Object.is(value, state[key])) return;
          Object.assign(state, { [key]: value });
          if (key === 'messages') state.historyVersion++;
          render();
        } });
      }
      for (const [name, key] of Object.entries({ turnRef: 'turn', questionRef: 'question', sendMark: 'sendMark', stopRequested: 'stopRequested', readbackRetry: 'readbackRetry' }) as [keyof Refs, keyof ConversationState][]) {
        Object.defineProperty(result, name, { configurable: true, enumerable: true, value: {
          get current() { return state[key]; }, set current(value: unknown) { Object.assign(state, { [key]: value }); },
        } });
      }
      scopes.set(id, result); return result;
    };
    ref.current = { records, requests, scope };
  }
  return ref.current;
}

/** Conflicting or unknown tags are never guessed. Only legacy untagged frames
 * may use the exactly-one-live-turn fallback. */
export function frameConversation(records: Map<string, ConversationState>, requests: Map<string, string>, data: Record<string, unknown>): string | null {
  const request = typeof data.request_id === 'string' && data.request_id ? data.request_id : null;
  const conversation = typeof data.conversation_id === 'string' && data.conversation_id ? data.conversation_id : null;
  const mapped = request ? requests.get(request) : null;
  if (mapped && conversation && mapped !== conversation) return null;
  if (mapped) return mapped;
  if (conversation) return conversation;
  if (request || conversation) return null;
  const live = [...records].filter(([, s]) => s.turn.stream || s.turn.pending);
  return live.length === 1 ? live[0][0] : null;
}

export function conversationLimit(error: PortalError): boolean {
  return /(?:concurr|live.?conversation|active.?stream|stream.?limit|too_many.*(?:turn|agent|conversation|stream))/.test(`${error.code} ${error.detail}`.toLowerCase());
}
export const CONVERSATION_LIMIT_LINE = 'Four agents are already working. Wait for one to finish or stop one.';
