import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import "./index.css";

console.log("Mobile PWA starting...");

const root = document.getElementById("root");
if (!root) {
  console.error("Root element not found!");
  document.body.innerHTML = "<h1 style='color: white;'>Error: Root element not found</h1>";
} else {
  console.log("Root element found, rendering app...");
  ReactDOM.createRoot(root).render(
    <React.StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </React.StrictMode>,
  );
}