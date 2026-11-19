import { useEffect, useState } from 'react';

export type PanelCollapse = { left: boolean; right: boolean };
export const panelStorageKey = (viewer: string) => `bob:panels:v1:${viewer}`;
export const agentConversationsKey = (viewer: string) => `bob:agent-conversations:v1:${viewer}`;
function readAgentConversations(viewer: string): Record<string, boolean> {
  try {
    const value = JSON.parse(window.localStorage.getItem(agentConversationsKey(viewer)) ?? 'null');
    if (value && typeof value === 'object' && !Array.isArray(value))
      return Object.fromEntries(Object.entries(value).filter(([, open]) => open === true).map(([agent]) => [agent, true]));
  } catch { /* Storage is optional, as it is for panel collapse. */ }
  return {};
}

/** Per account and agent on this PC; searching never writes this preference. */
export function useAgentConversations(viewer: string) {
  const [state, setState] = useState(() => ({ viewer, value: readAgentConversations(viewer) }));
  useEffect(() => { setState({ viewer, value: readAgentConversations(viewer) }); }, [viewer]);
  useEffect(() => {
    if (state.viewer === viewer) try { window.localStorage.setItem(agentConversationsKey(viewer), JSON.stringify(state.value)); } catch { /* optional storage */ }
  }, [viewer, state]);
  return {
    opened: state.viewer === viewer ? state.value : readAgentConversations(viewer),
    toggle: (agent: string) => setState(prior => {
      const value = prior.viewer === viewer ? prior.value : readAgentConversations(viewer);
      return { viewer, value: { ...value, [agent]: value[agent] !== true } };
    }),
  };
}

export function readPanelCollapse(viewer: string): PanelCollapse {
  try {
    const value = JSON.parse(window.localStorage.getItem(panelStorageKey(viewer)) ?? 'null');
    if (value && typeof value.left === 'boolean' && typeof value.right === 'boolean') return value;
  } catch { /* Storage may be disabled; controls still work in memory. */ }
  const narrow = window.matchMedia?.('(max-width: 900px)').matches ?? window.innerWidth <= 900;
  return { left: narrow, right: narrow };
}
export function panelShortcut(event: KeyboardEvent): keyof PanelCollapse | null {
  const target = event.target;
  if (event.defaultPrevented || event.repeat || !event.ctrlKey || event.metaKey || event.shiftKey || event.key.toLowerCase() !== 'b' ||
    target instanceof window.Element && target.closest('.monaco-editor, .xterm, [contenteditable="true"], [data-panel-shortcuts="off"]')) return null;
  return event.altKey ? 'right' : 'left';
}
export function usePanelCollapse(viewer: string) {
  const [state, setState] = useState(() => ({ viewer, value: readPanelCollapse(viewer) }));
  useEffect(() => { setState({ viewer, value: readPanelCollapse(viewer) }); }, [viewer]);
  useEffect(() => {
    if (state.viewer === viewer) try { window.localStorage.setItem(panelStorageKey(viewer), JSON.stringify(state.value)); } catch { /* optional storage */ }
  }, [viewer, state]);
  const toggle = (side: keyof PanelCollapse) => setState(prior => {
    const value = prior.viewer === viewer ? prior.value : readPanelCollapse(viewer);
    return { viewer, value: { ...value, [side]: !value[side] } };
  });
  useEffect(() => {
    const key = (event: KeyboardEvent) => { const side = panelShortcut(event); if (side) { event.preventDefault(); toggle(side); } };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [viewer]);
  return { collapsed: state.viewer === viewer ? state.value : readPanelCollapse(viewer), toggle };
}
