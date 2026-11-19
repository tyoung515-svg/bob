import { useEffect, useRef, useState } from 'react';

export function YoloConfirm({ onConfirm, onCancel }: { onConfirm: () => void; onCancel: () => void }) {
  const [understood, setUnderstood] = useState(false);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { cancel.current?.focus(); }, []);
  return <div className="owner-modal-backdrop" onKeyDown={e => {
    if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
    if (e.key === 'Tab') {
      const controls = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('input,button:not(:disabled)'));
      const index = controls.indexOf(document.activeElement as HTMLElement);
      e.preventDefault(); controls[(index + (e.shiftKey ? controls.length - 1 : 1)) % controls.length]?.focus();
    }
  }}><section className="set-card owner-modal" role="dialog" aria-modal="true" aria-label="Enable yolo risk confirmation">
    <h2>Enable yolo?</h2>
    <p>Builder may delete or overwrite workspace files, run arbitrary code, download packages and use the network, and spend model calls. Mistakes are possible.</p>
    <p>Actions remain confined to the project workspace sandbox. Yolo expires in 2 hours or at the end of this conversation session, whichever comes first, and reverts to Auto.</p>
    <label><input type="checkbox" checked={understood} onChange={e => setUnderstood(e.target.checked)} />I understand</label>
    <button type="button" disabled={!understood} onClick={() => { if (understood) onConfirm(); }}>Enable yolo</button>
    <button ref={cancel} type="button" onClick={onCancel}>Cancel</button>
  </section></div>;
}
