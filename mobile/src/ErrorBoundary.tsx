import React, { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("ErrorBoundary caught an error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: 20, color: "white", background: "#0b0e14", minHeight: "100vh" }}>
          <h1>Something went wrong</h1>
          <pre style={{ color: "#ef5350", whiteSpace: "pre-wrap" }}>
            {this.state.error?.message}
          </pre>
          <pre style={{ color: "#8a94a6", whiteSpace: "pre-wrap", fontSize: 12 }}>
            {this.state.error?.stack}
          </pre>
        </div>
      );
    }
    return this.props.children;
  }
}