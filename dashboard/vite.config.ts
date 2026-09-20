/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served by the engine under /dashboard/ from engine/static, so the
// base path and build output are set accordingly. In dev, /api and /ws are
// proxied to a locally running engine on :8000.
export default defineConfig({
  base: "/dashboard/",
  plugins: [react()],
  build: {
    outDir: "../engine/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/admin": "http://127.0.0.1:8000",
      "/metrics": "http://127.0.0.1:8000",
      "/logs": "http://127.0.0.1:8000",
      "/readyz": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    css: false,
  },
});
