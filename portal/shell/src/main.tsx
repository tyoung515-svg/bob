import React from 'react';
import { invoke } from '@tauri-apps/api/core';
import ReactDOM from 'react-dom/client';
import { PortalShell as Shell } from './portal/PortalShell';
import { toPortalError } from './portal/portal-protocol';
import './styles.css';
import './workbench.css';

// FunctionalApp / WorkbenchApp is available only through a developer-enabled profile.
const root = ReactDOM.createRoot(document.getElementById('root') as HTMLElement);
invoke<boolean>('app_developer_mode')
  .then(() => {
    root.render(<React.StrictMode><Shell invoke={invoke} /></React.StrictMode>);
  })
  .catch((error) => {
    // Developer-mode detection failing must not hide the product surface.
    console.warn('developer-mode detection failed', toPortalError(error).detail);
    root.render(
      <React.StrictMode>
        <Shell invoke={invoke} />
      </React.StrictMode>,
    );
  });
