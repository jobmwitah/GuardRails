import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = process.env.VITE_BACKEND_URL || "https://spooky-impulsive-sandy.ngrok-free.dev";

export default defineConfig({
  plugins: [react()],
  define: {
    __MOBILE_IS_PROD__: JSON.stringify(process.env.NODE_ENV === "production"),
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