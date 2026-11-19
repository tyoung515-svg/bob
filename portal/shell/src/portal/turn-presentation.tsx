import type { TerminalNote } from './portal-state';

/** Only explicit completion/history metadata marks a reply unfinished. */
export function ReplyStatus({ stoppedReason, extensions, disabled, onContinue, latest = true }: {
  stoppedReason?: unknown; extensions?: unknown; disabled: boolean; onContinue: () => void; latest?: boolean;
}) {
  const unfinished = stoppedReason === 'tool_round_limit';
  const count = typeof extensions === 'number' && Number.isInteger(extensions) && extensions > 0 ? extensions : 0;
  if (!unfinished && !count) return null;
  return <div className="reply-status">
    {unfinished && <><small>Not finished: it reached the step limit for one reply.</small>{latest && <button type="button" disabled={disabled} onClick={onContinue}>Continue</button>}</>}
    {count > 0 && <small>Extended {count} {count === 1 ? 'time' : 'times'}.</small>}
  </div>;
}

/** Presentation only: the stored question/answer and the outbound payload stay intact. */
export function UserAnswer({ content }: { content: string }) {
  const match = /^Operator answer to: ([\s\S]+?)\r?\n([\s\S]*)$/.exec(content);
  return match ? <>
    <div className="answered-question"><small>You answered</small><blockquote>{match[1]}</blockquote></div>
    <p className="content">{match[2]}</p>
  </> : <p className="content">{content}</p>;
}

export function plainTurnEnd(note: TerminalNote): string | null {
  if (note.code === 'approval_expired') return 'The approval request expired before you answered. What Bob wrote is kept above; send a message to carry on.';
  if (note.unanswered_question && note.kind !== 'complete') return 'The turn ended while Bob was waiting for your answer. What it wrote is kept above; send a message to carry on.';
  if (note.code === 'stream_deadline') return 'Bob ran out of time on this turn. What it wrote is kept above; send a message to carry on.';
  return null;
}
