import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: "./tests/e2e/global-setup.ts",
  timeout: 15000,
  use: {
    browserName: "firefox",
    headless: true,
    baseURL: "http://127.0.0.1:5000",
  },
});
