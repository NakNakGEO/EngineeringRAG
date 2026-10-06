import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API is proxied so the browser sees one origin (no CORS, SSE works).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.EIOS_API_URL ?? "http://127.0.0.1:8000", rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  test: { environment: "node" },
});
