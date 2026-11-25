import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const backend = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../backend/surplus/static", emptyOutDir: true },
  server: {
    port: 5173,
    // changeOrigin rewrites Host to 127.0.0.1:8765, which the API's DNS-rebinding check accepts.
    proxy: { "/api": { target: backend, changeOrigin: true }, "/auth": { target: backend, changeOrigin: true } },
  },
  test: { environment: "jsdom", include: ["src/**/*.test.ts?(x)"] },
});
