import React from 'react';
import { txt } from './portal-protocol';

type Props = { children: React.ReactNode; recovery: React.ReactNode };
type State = { error: string | null };

/**
 * A render fault in the portal surface must never leave the owner with a blank
 * window and no explanation. Nothing is retried or sent from here: the message
 * says the surface stopped drawing and offers a redraw or a window reload.
 */
export class PortalBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: txt(error instanceof Error ? error.message : error, 'unknown render fault') };
  }

  componentDidCatch(error: unknown) {
    console.error('portal render fault', error);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="portal">
          <div role="alert" className="portal-fault banner">
            <p>The portal surface hit a rendering fault and stopped drawing. No request was sent by this fault.</p>
            <p className="hint">{this.state.error}</p>
            {this.props.recovery}
            <button type="button" onClick={() => this.setState({ error: null })}>
              Try to redraw
            </button>
            <button type="button" onClick={() => window.location.reload()}>
              Reload the app window
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}
