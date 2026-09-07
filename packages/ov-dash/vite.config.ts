import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

/**
 * The client build.
 *
 * In development Vite serves the UI and forwards `/api` and `/auth` to the Hono
 * process, so the login redirect and the cookie behave exactly as they will in
 * production, where one process serves both.
 */
export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: "dist/client",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/auth": { target: "http://localhost:8080", changeOrigin: true },
    },
  },
});
