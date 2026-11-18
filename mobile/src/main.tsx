import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// Service Worker Registration for PWA support in production builds
// Vite replaces __MOBILE_IS_PROD__ at build time
/* eslint-disable @typescript-eslint/no-unsafe-member-access */
const isProduction = (__MOBILE_IS_PROD__ as unknown as boolean) ?? false;

if ("serviceWorker" in navigator && isProduction) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .then((reg) => {
        console.log("Service Worker registered with scope:", reg.scope);
      })
      .catch((err) => {
        console.warn("Service Worker registration failed:", err);
      });
  });
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);