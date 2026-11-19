import { useEffect, useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';

export type ModelProvider = { id: string; label: string; method: 'signin' | 'key' };

/** Enrollment capabilities come from the native validator, never a UI roster. */
export function useModelProviders(invoke: NativeInvoke) {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    setProviders([]); setError('');
    void invoke<ModelProvider[]>('runtime_model_providers').then(
      (rows) => { if (alive) setProviders(rows); },
      () => { if (alive) setError('Provider setup choices could not be loaded.'); },
    );
    return () => { alive = false; };
  }, [invoke]);
  return { providers, error };
}

export function backendLabel(id: string, label?: string | null): string {
  if (id === 'openai_codex') return "GPT (ChatGPT sign-in), chat with Bob's tools";
  if (id === 'codex_code') return 'Codex (coding agent)';
  return label || id;
}
