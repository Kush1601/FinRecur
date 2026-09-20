import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // This is an intentionally full-stack demo path: it streams two batches,
  // groups exceptions, generates explanations, applies a fix, and verifies it.
  timeout: 300_000,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3100",
  },
  webServer: process.env.E2E === "1" ? [
    {
      command: "cd ../backend && uv run uvicorn api.main:app --port 8100",
      url: "http://localhost:8100/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "NEXT_PUBLIC_API_URL=http://localhost:8100 npm run dev -- --port 3100",
      url: "http://localhost:3100",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ] : undefined,
});
