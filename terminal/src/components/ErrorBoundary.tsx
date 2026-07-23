"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = {
  children: ReactNode;
  label?: string;
};

type State = {
  error: Error | null;
};

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[Terminal ErrorBoundary]", error, info.componentStack);
  }

  private reload = (): void => {
    this.setState({ error: null });
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex h-screen flex-col items-center justify-center gap-4 bg-[#050505] p-6 text-center">
          <p className="cq-mono text-sm text-[#ff0055]">
            {this.props.label ?? "Terminal"} crashed
          </p>
          <p className="max-w-lg cq-mono text-[11px] text-[#a1a1aa]">
            {this.state.error.message}
          </p>
          <button
            type="button"
            onClick={this.reload}
            className="cq-mono rounded border border-[#00f5d4] px-4 py-2 text-xs text-[#00f5d4] hover:bg-[#00f5d4]/10"
          >
            Reload terminal
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
