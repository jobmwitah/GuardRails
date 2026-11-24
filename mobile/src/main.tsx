import React from "react";
import ReactDOM from "react-dom/client";
import { IconContext } from "@phosphor-icons/react";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import "./index.css";

console.log("Mobile PWA starting...");

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .catch((err) => console.warn("Service worker registration failed:", err));
  });
}

const root = document.getElementById("root");
if (!root) {
  console.error("Root element not found!");
  document.body.innerHTML = "<h1 style='color: white;'>Error: Root element not found</h1>";
} else {
  console.log("Root element found, rendering app...");
  ReactDOM.createRoot(root).render(
    <React.StrictMode>
      <IconContext.Provider value={{ weight: "regular", size: 18, color: "currentColor" }}>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </IconContext.Provider>
    </React.StrictMode>,
  );
}