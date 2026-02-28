import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: "./tests/e2e/global-setup.ts",
  globalTeardown: "./tests/e2e/global-teardown.ts",
  timeout: 30000,
  retries: 1,
  use: {
    browserName: "firefox",
    headless: true,
    baseURL: "http://127.0.0.1:5000",
  },
});
