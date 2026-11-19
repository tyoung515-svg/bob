import { ReadFaultText } from './provider-restart';
import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { BuilderPreviewData } from './portal-protocol';
import { toPortalError } from './portal-protocol';

export const BUILDER_PREVIEW_CAP = 5 * 1024 * 1024;
export const STATIC_IN_APP_PREVIEW_CSP = "default-src 'none'; script-src 'none'; connect-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
/** Parse in an inert template; detect active HTML before the static sanitizer removes it. */
export function hasPreviewScripts(html: string): boolean {
  const template = document.createElement('template');
  template.innerHTML = html;
  const active = (root: DocumentFragment): boolean => Array.from(root.querySelectorAll('*')).some(node =>
    node.localName === 'script' || Array.from(node.attributes).some(attribute => /^on/i.test(attribute.name)) ||
    node.localName === 'template' && active((node as HTMLTemplateElement).content));
  return active(template.content);
}
export const rasterMime = (mime: string) => ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(mime);
export const textMime = (mime: string) => mime.startsWith('text/') || ['application/json', 'application/xml', 'application/javascript'].includes(mime);

// Keep markup and formatting; strip active elements and navigation. CSP also
// blocks CSS URLs/imports, remote images, fonts and other network requests.
export function previewHtml(html: string): string {
  // Template contents remain inert while parsing, including resource URLs.
  // Do not parse into a browsing document before removing those attributes.
  const template = document.createElement('template');
  template.innerHTML = html;
  template.content.querySelectorAll('script,iframe,frame,object,embed,link,meta,base').forEach(node => node.remove());
  template.content.querySelectorAll('*').forEach(node => {
    for (const attribute of Array.from(node.attributes)) {
      if (!['class', 'style', 'id', 'title', 'alt', 'width', 'height', 'colspan', 'rowspan', 'dir', 'lang', 'role'].includes(attribute.name) && !attribute.name.startsWith('aria-') &&
          !(node.tagName === 'IMG' && attribute.name === 'src' && /^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(attribute.value))) node.removeAttribute(attribute.name);
    }
  });
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${STATIC_IN_APP_PREVIEW_CSP}"></head><body>${template.innerHTML}</body></html>`;
}

export function decodePreview(data: BuilderPreviewData): { text: string; image: string | null } {
  if (typeof data.mime !== 'string' || !Number.isSafeInteger(data.size) || data.size < 0 || typeof data.truncated !== 'boolean' ||
      (data.bytes_b64 !== null && (typeof data.bytes_b64 !== 'string' || data.bytes_b64.length > Math.ceil(BUILDER_PREVIEW_CAP / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(data.bytes_b64)))) throw new Error('Invalid preview data.');
  if (data.bytes_b64 === null) return { text: 'No preview bytes returned by the server.', image: null };
  const bytes = Uint8Array.from(atob(data.bytes_b64), c => c.charCodeAt(0));
  if (bytes.length > BUILDER_PREVIEW_CAP || (!data.truncated && bytes.length !== data.size)) throw new Error('Invalid preview size.');
  return { text: textMime(data.mime) ? new TextDecoder().decode(bytes) : 'This file type has no inline preview.', image: rasterMime(data.mime) ? `data:${data.mime};base64,${data.bytes_b64}` : null };
}

function PreviewText({ text, path }: { text: string; path: string }) {
  const [highlighted, setHighlighted] = useState('');
  useEffect(() => {
    let live = true; setHighlighted('');
    // Reuse the IDE's existing Monaco tokenizer; large files stay readable
    // without spending seconds colorizing megabytes on the UI thread.
    if (text.length <= 256 * 1024) void (async () => {
      const monaco = await import('monaco-editor/esm/vs/editor/editor.api');
      await import('monaco-editor/esm/vs/basic-languages/monaco.contribution');
      const extension = path.split('.').pop()?.toLowerCase();
      const language = ({ py: 'python', js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript', json: 'json', css: 'css', md: 'markdown', rs: 'rust', sh: 'shell', ps1: 'powershell', yaml: 'yaml', yml: 'yaml', xml: 'xml', html: 'html' } as Record<string, string>)[extension ?? ''] ?? 'plaintext';
      monaco.editor.setTheme('vs-dark');
      const html = await monaco.editor.colorize(text, language, { tabSize: 4 });
      if (live) setHighlighted(html);
    })().catch(() => { /* plaintext remains available when tokenizer is unavailable */ });
    return () => { live = false; };
  }, [text, path]);
  const style = { maxHeight: '24rem', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const;
  // Monaco escapes source text before producing its trusted token markup.
  return highlighted ? <pre className="monaco-colorized vs-dark" style={style} dangerouslySetInnerHTML={{ __html: highlighted }} /> : <pre style={style}>{text}</pre>;
}

export function BuilderPreview({ client, conversationId, initialPath = '', autoLoad = false, readOnlyPath = false }: { client: PortalClient; conversationId: string; initialPath?: string; autoLoad?: boolean; readOnlyPath?: boolean }) {
  const [path, setPath] = useState(initialPath);
  const [data, setData] = useState<BuilderPreviewData | null>(null);
  const [decoded, setDecoded] = useState<{ text: string; image: string | null } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [opened, setOpened] = useState(false);
  const epoch = useRef(0);
  useEffect(() => { epoch.current++; setData(null); setDecoded(null); return () => { epoch.current++; }; }, [client, conversationId]);
  useEffect(() => { if (autoLoad && initialPath) void load(); }, [client, conversationId, initialPath, autoLoad]);
  async function load() {
    const generation = ++epoch.current;
    setBusy(true); setData(null); setDecoded(null); setError(''); setOpened(false);
    try {
      const value = await client.builderPreview(conversationId, path);
      if (value.path !== path) throw new Error('Preview path did not match the request.');
      const body = decodePreview(value);
      if (generation === epoch.current) { setData(value); setDecoded(body); }
    } catch (e) { if (generation === epoch.current) setError(JSON.stringify(toPortalError(e))); }
    finally { if (generation === epoch.current) setBusy(false); }
  }
  async function open() {
    if (!data || busy) return;
    const generation = epoch.current;
    setBusy(true); setError('');
    try {
      // Native re-fetches the scoped file. IPC cannot provide local paths/bytes.
      const result = await client.builderOpenCopy(conversationId, data.path);
      if (!result.opened) throw new Error('The default handler did not open.');
      if (generation === epoch.current) setOpened(true);
    } catch (e) { if (generation === epoch.current) setError(toPortalError(e).detail); }
    finally { if (generation === epoch.current) setBusy(false); }
  }
  return <section aria-label="Builder file preview" style={{ minWidth: 0 }}>
    <form onSubmit={e => { e.preventDefault(); void load(); }}>
      <label>Workspace path <input aria-label="Preview path" value={path} readOnly={readOnlyPath} disabled={busy} onChange={e => setPath(e.target.value)} placeholder="index.html" /></label>
      <button type="submit" disabled={busy || !path.trim()}>Preview</button>
    </form>
    {data && decoded && <>
      <p>{data.path} · {data.mime} · {data.size} bytes{data.truncated ? ' · Truncated' : ''}</p>
      <p>Workspace file{data.provenance?.modified_at ? `, modified ${new Date(data.provenance.modified_at * 1000).toISOString()}` : ''}. Current lane workspace; checked snapshots and publication receipts describe their recorded revisions.</p>
      {data.provenance && <details><summary>Details</summary><pre>{JSON.stringify(data.provenance, null, 2)}</pre></details>}
      {data.mime === 'text/html' && hasPreviewScripts(decoded.text) && <p className="preview-script-notice" role="note">This in-app preview is static. Use <strong>Open on this PC</strong> to try it.</p>}
      {decoded.image ? <img src={decoded.image} alt={data.path} style={{ maxWidth: '100%' }} /> : data.mime === 'text/html' && data.bytes_b64 !== null ?
        <iframe title="Workspace HTML preview" sandbox="" referrerPolicy="no-referrer" srcDoc={previewHtml(decoded.text)} style={{ width: '100%', height: '24rem', background: 'white', border: 0 }} /> :
        <PreviewText text={decoded.text} path={data.path} />}
      <button type="button" disabled={busy || data.truncated || data.bytes_b64 === null || !(textMime(data.mime) || rasterMime(data.mime))} onClick={() => void open()}>Open on this PC</button>
      <p>Opens a temporary copy in your default browser. HTML runs scripts, forms and dialogs in a confined sandbox with temporary in-memory storage and no network access; this in-app preview stays static. Code files open as text.</p>
    </>}
    {opened && <p role="status">Opened a temporary copy.</p>}{error && <p role="alert"><ReadFaultText client={client} error={error} /></p>}
  </section>;
}
