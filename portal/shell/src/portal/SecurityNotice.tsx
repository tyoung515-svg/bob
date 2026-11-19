import { useEffect, useState } from 'react';
export function SecurityNotice() {
  const [notice, setNotice] = useState('');
  useEffect(() => {
    const receive = (event: Event) => {
      const text: unknown = (event as CustomEvent<unknown>).detail;
      if (typeof text === 'string' && /^two-factor was reset on [0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(text)) setNotice(text);
    };
    window.addEventListener('bob-security-notice', receive);
    return () => window.removeEventListener('bob-security-notice', receive);
  }, []);
  return notice ? <p role="status" className="notice">{notice}</p> : null;
}
