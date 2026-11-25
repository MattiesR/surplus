import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:8799" },
  webServer: {
    command: "sh e2e/server.sh",
    url: "http://127.0.0.1:8799/api/health",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
