import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = (process.env.VITE_BACKEND_URL || "https://spooky-impulsive-sandy.ngrok-free.dev").replace(/\/+$/, "");

export default defineConfig({
  plugins: [react()],
  define: {
    __MOBILE_IS_PROD__: JSON.stringify(process.env.NODE_ENV === "production"),
    // Baked into the bundle so the Android/iOS shell (served from
    // https://localhost by Capacitor) can reach the backend. The deployed PWA
    // ignores this and keeps using same-origin relative URLs.
    __MOBILE_BACKEND_ORIGIN__: JSON.stringify(BACKEND),
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        secure: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});