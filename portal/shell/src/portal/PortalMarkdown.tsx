import { Fragment, useContext, useState, type ReactNode } from 'react';
import { ActionCard, ActionCardsContext, validateAction, type ActionContext } from './ActionCard';
import { useExperience } from './experience';
import { useWorkspaceFiles } from './WorkspaceFileLinks';

function FileText({ text }: { text: string }) {
  const files = useWorkspaceFiles();
  // Longest match first, with path boundaries; unconfirmed names stay plain text.
  const paths = [...files.paths].sort((a, b) => b.length - a.length);
  if (!paths.length) return <>{text}</>;
  const escaped = paths.map(path => path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp(`(^|[^\\w./\\\\-])(${escaped.join('|')})(?=$|[^\\w./\\\\-]|\\.(?=$|[^\\w./\\\\-]))`, 'g');
  const out: ReactNode[] = []; let from = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index! + match[1].length;
    out.push(text.slice(from, at), <button key={at} type="button" role="link" className="workspace-file-link" onClick={() => files.open(match[2])}>{match[2]}</button>);
    from = at + match[2].length;
  }
  out.push(text.slice(from));
  return <>{out}</>;
}

/**
 * Portal transcript renderer.
 *
 * A deliberately small Markdown subset. React escapes every string: no HTML
 * parsing, no image tags, no URL fetching, no model-supplied hrefs. What the
 * server sends is what the operator reads — only its *shape* is improved.
 *
 * The one portal-specific behaviour is `### Subtask N`: a fan-out answer arrives
 * as several concatenated worker outputs, and rendering them as five open
 * headings buries the answer. Each subtask section collapses into a summary row
 * so the reply reads as a short list first and full text on demand.
 */

function inline(text: string): ReactNode[] {
  return text
    .split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g)
    .map((part, i) =>
      part.startsWith('`') ? (
        <code key={i}><FileText text={part.slice(1, -1)} /></code>
      ) : part.startsWith('**') ? (
        <strong key={i}><FileText text={part.slice(2, -2)} /></strong>
      ) : (
        <Fragment key={i}><FileText text={part.replace(/!\[([^\]]*)\]\([^)]*\)/g, '[Image disabled: $1]')} /></Fragment>
      ),
    );
}

function CodeBlock({ text, language }: { text: string; language: string }) {
  const [status, setStatus] = useState('Copy');
  return (
    <div className="md-code">
      <div className="md-code-head">
        <span>{language || 'code'}</span>
        <button
          type="button"
          onClick={() => {
            if (!navigator.clipboard) {
              setStatus('Copy unavailable');
              return;
            }
            void navigator.clipboard.writeText(text).then(
              () => setStatus('Copied'),
              () => setStatus('Copy failed'),
            );
          }}
        >
          {status}
        </button>
      </div>
      <pre>
        <code>{text}</code>
      </pre>
    </div>
  );
}

function ToolResult({ text, language }: { text: string; language: string }) {
  let summary = `${language || 'Code'} · ${text.split('\n').length} lines`;
  let diff: string | null = null;
  try {
    const value = JSON.parse(text);
    const result = value.result ?? value;
    const path = result.path ?? result.detail?.path;
    diff = result.diff ?? result.detail?.diff ?? null;
    if (typeof path === 'string') summary = `${result.status === 'completed' ? 'Wrote' : 'File'} ${path}${typeof diff === 'string' ? ` · +${diff.split('\n').filter((s: string) => s.startsWith('+') && !s.startsWith('+++')).length} lines` : ''}`;
    else if (typeof result.status === 'string') summary = `Action ${result.status}`;
  } catch { /* ordinary code */ }
  if (language === 'diff') {
    const path = /^\+\+\+ (?:b\/)?(.+)$/m.exec(text)?.[1];
    summary = `${path ? `Updated ${path}` : 'Changes'} · +${text.split('\n').filter(s => s.startsWith('+') && !s.startsWith('+++')).length} lines`;
  }
  return <div className="tool-result"><p>{summary}</p>
    {typeof diff === 'string' && <details><summary>Changes</summary><CodeBlock text={diff} language="diff" /></details>}
    <details><summary>Details</summary><CodeBlock text={text} language={language} /></details>
  </div>;
}

function FencedBlock({ text, language }: { text: string; language: string }) {
  const simple = useExperience() === 'simple';
  if (simple) return <details className="md-code-fold"><summary>Code · {language || 'code'} · {text ? text.split('\n').length : 0} lines · Show</summary><CodeBlock text={text} language={language} /></details>;
  return text.split('\n').length > 20 || language === 'diff' || language === 'json' ? <ToolResult text={text} language={language} /> : <CodeBlock text={text} language={language} />;
}

// Incomplete opening fences are held until the language is known. Protocol
// bodies (including unfinished ones) never enter the transcript renderer.
export function readableTranscript(text: string, actions = false): string {
  return text.replace(/(^|\n)[ \t]*```([^\n]*)(?:\n([\s\S]*?)(?:\n[ \t]*```[^\n]*(?=\n|$)|$)|$)/g,
    (whole, prefix: string, language: string) => {
      const tag = language.trim();
      return (/^bob-/i.test(tag) && !(actions && tag === 'bob-action')) || !whole.includes('\n', prefix.length) ? prefix : whole;
    }).replace(/(^|\n)[ \t]*`{1,2}$/, '$1');
}

function Table({ head, rows }: { head: string[]; rows: string[][] }) {
  return (
    <div className="md-table">
      <table>
        <thead>
          <tr>
            {head.map((cell, i) => (
              <th key={i}>{inline(cell)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r}>
              {head.map((_, c) => (
                <td key={c}>{inline(row[c] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const isRule = (line: string) => /^\s*([-*_])\1{2,}\s*$/.test(line);
const isHeading = (line: string) => /^#{1,4} /.test(line);
const isSubtask = (line: string) => /^#{1,4}\s+Subtask\s+\d+/i.test(line);
const isUl = (line: string) => /^\s*[-*+] /.test(line);
const isOl = (line: string) => /^\s*\d+[.)] /.test(line);
const isFence = (line: string) => line.trimStart().startsWith('```');
const isDetailsHeading = (line: string) => /^(?:Details:?|\*\*Details:?\*\*:?)$/i.test(line.trim().replace(/^#{1,6}\s+/, '').replace(/\s+#+$/, ''));
const isDetailsTag = (line: string) => /^\s*<details>/.test(line);
const isQuote = (line: string) => /^\s*> /.test(line);
const isTableHead = (line: string, next: string | undefined) =>
  line.trimStart().startsWith('|') && !!next && /^\s*\|?[\s:|-]+\|?\s*$/.test(next) && next.includes('-');

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

/** Only these literal delimiters are recognised. The contents remain escaped
 * Markdown, including any other HTML. Ignore delimiters in fenced examples. */
function detailsBlock(lines: string[], start: number): { body: string; end: number; tail: string } | null {
  let depth = 0, fenced = false;
  const body: string[] = [];
  for (let at = start; at < lines.length; at += 1) {
    const line = lines[at];
    if (at > start && isFence(line)) fenced = !fenced;
    if (!fenced) for (const tag of line.matchAll(/<details>|<\/details>/g)) {
      depth += tag[0] === '<details>' ? 1 : -1;
      if (depth === 0) {
        body.push(line.slice(at === start ? line.indexOf('<details>') + 9 : 0, tag.index));
        return { body: body.join('\n').replace(/^\s*<summary>[\s\S]*?<\/summary>\s*/, ''), end: at, tail: line.slice(tag.index! + tag[0].length) };
      }
    }
    body.push(at === start ? line.slice(line.indexOf('<details>') + 9) : line);
  }
  return null;
}

/** Split the reply into blocks. `:::options` and ```options fences are removed
 *  here because the caller renders them as buttons instead of code. */
function blocks(text: string, actions?: { context: ActionContext; simple: boolean; index: number }, outside?: ReactNode[]): ReactNode[] {
  const lines = text.split('\n');
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  function fold(title: string, text: string) {
    const visible: ReactNode[] = [];
    const body = blocks(text, actions, visible);
    if (body.length) out.push(<details key={key++} className="md-subtask"><summary>{title}</summary><div className="md-subtask-body">{body}</div></details>);
    // Bubble through nested folds without reparsing or changing action indices.
    (outside ?? out).push(...visible);
  }
  while (i < lines.length) {
    const line = lines[i];
    const next = lines[i + 1];

    if (line.trim() === '') {
      i += 1;
      continue;
    }

    if (isFence(line)) {
      const language = line.trim().slice(3).trim();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trimStart().startsWith('```')) body.push(lines[i++]);
      const complete = i < lines.length;
      if (complete) i += 1;
      if (language === 'bob-action' && actions) {
        const index = actions.index++;
        const action = complete && index < 2 ? validateAction(body.join('\n'), actions.context.faces) : null;
        if (action) (outside ?? out).push(<ActionCard key={`action-${index}`} action={action} index={index} />);
        else if (!complete && index < 2 && actions.context.streaming) (outside ?? out).push(<p key={`action-${index}`}>An action is being prepared.</p>);
        else if (!actions.simple && (complete || !actions.context.streaming)) out.push(<CodeBlock key={key++} language={language} text={body.join('\n')} />);
      } else if (!/^options?$/i.test(language) && !language.startsWith('bob-')) {
        const text = body.join('\n');
        out.push(<FencedBlock key={key++} language={language} text={text} />);
      }
      continue;
    }

    if (line.trim() === ':::options') {
      i += 1;
      while (i < lines.length && lines[i].trim() !== ':::') i += 1;
      if (i < lines.length) i += 1;
      continue;
    }

    if (isDetailsTag(line)) {
      const section = detailsBlock(lines, i);
      if (section) {
        fold('Details', section.body);
        i = section.end;
        lines[i] = section.tail;
        if (!section.tail.trim()) i += 1;
        continue;
      }
    }

    if (isDetailsHeading(line)) {
      fold('Details', lines.slice(i + 1).join('\n'));
      break;
    }

    // The same closing section written as one paragraph: "Details: the tool I used…".
    // Only at the start of a paragraph, so a sentence that mentions details is untouched.
    const inlineDetails = (i === 0 || lines[i - 1].trim() === '') ? /^\s*(?:\*\*)?Details:(?:\*\*)?\s+(\S.*)$/.exec(line) : null;
    if (inlineDetails) {
      fold('Details', [inlineDetails[1], ...lines.slice(i + 1)].join('\n'));
      break;
    }

    if (isRule(line)) {
      out.push(<hr key={key++} />);
      i += 1;
      continue;
    }

    // A subtask section collapses: heading line is the summary, the body runs to
    // the next heading of any level.
    if (isSubtask(line)) {
      const title = line.replace(/^#{1,4}\s+/, '').trim();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !isHeading(lines[i])) body.push(lines[i++]);
      fold(title, body.join('\n'));
      continue;
    }

    if (isHeading(line)) {
      const level = (line.match(/^#+/) ?? ['#'])[0].length;
      const content = inline(line.replace(/^#{1,4}\s+/, ''));
      out.push(
        level <= 2 ? (
          <h3 key={key++}>{content}</h3>
        ) : (
          <h4 key={key++}>{content}</h4>
        ),
      );
      i += 1;
      continue;
    }

    if (isTableHead(line, next)) {
      const head = cells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trimStart().startsWith('|')) rows.push(cells(lines[i++]));
      out.push(<Table key={key++} head={head} rows={rows} />);
      continue;
    }

    if (isUl(line) || isOl(line)) {
      const ordered = isOl(line);
      // Keep the author's numbering: a blank line between items splits the run,
      // and restarting every fragment at 1 turns "1. / 2." into "1. / 1.".
      const start = ordered ? Number.parseInt(line.trim(), 10) : 1;
      const items: string[] = [];
      while (i < lines.length && (ordered ? isOl(lines[i]) : isUl(lines[i]))) {
        items.push(lines[i++].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ''));
      }
      out.push(
        ordered ? (
          <ol key={key++} start={Number.isFinite(start) ? start : 1}>
            {items.map((item, n) => (
              <li key={n}>{inline(item)}</li>
            ))}
          </ol>
        ) : (
          <ul key={key++}>
            {items.map((item, n) => (
              <li key={n}>{inline(item)}</li>
            ))}
          </ul>
        ),
      );
      continue;
    }

    if (isQuote(line)) {
      const quoted: string[] = [];
      while (i < lines.length && isQuote(lines[i])) quoted.push(lines[i++].replace(/^\s*> ?/, ''));
      out.push(
        <blockquote key={key++}>{inline(quoted.join(' '))}</blockquote>,
      );
      continue;
    }

    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !isFence(lines[i]) &&
      !(para.length > 0 && (isDetailsTag(lines[i]) || isDetailsHeading(lines[i]))) &&
      !isHeading(lines[i]) &&
      !isUl(lines[i]) &&
      !isOl(lines[i]) &&
      !isQuote(lines[i]) &&
      !isRule(lines[i]) &&
      !isTableHead(lines[i], lines[i + 1])
    ) {
      para.push(lines[i++]);
    }
    if (para.length) {
      const text = para.join('\n');
      if (/\b(?:[a-f0-9]{64}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\b/i.test(text)) {
        out.push(<details key={key++}><summary>Details</summary><p>{inline(text)}</p></details>);
      } else out.push(<p key={key++}>{inline(text)}</p>);
    }
    else i += 1;
  }
  return out;
}

export function PortalMarkdown({ text, role }: { text: string; role?: string }) {
  const context = useContext(ActionCardsContext), simple = useExperience() === 'simple';
  const actions = role === 'assistant' && context ? { context, simple, index: 0 } : undefined;
  return <div className="content md">{blocks(readableTranscript(text, !!actions), actions)}</div>;
}
