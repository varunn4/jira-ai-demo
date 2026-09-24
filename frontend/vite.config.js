import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// The SPA is served by FastAPI from `frontend/dist` at the site root.
// During local development, `npm run dev` proxies API calls to uvicorn so the
// React dev server (5173) and the FastAPI app (8000) behave like one origin.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/auth": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/graph-admin": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/analyze-ticket": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/rca": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ring-studio": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/zoho": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/repo-tree": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/static": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/rings": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
