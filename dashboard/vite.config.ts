import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Relative base so the built assets resolve from FastAPI's static mount.
// Dev: proxy /api and /media to cuadra-core (no CORS, same origin feel).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8321",
      "/media": "http://127.0.0.1:8321",
    },
  },
  build: {
    outDir: "../backend/cuadra/web_dashboard",
    emptyOutDir: true,
  },
});
