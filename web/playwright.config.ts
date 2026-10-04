import { defineConfig, devices } from "@playwright/test";

import {
  ADMIN_LOGIN,
  API_PORT,
  API_URL,
  BASE_URL,
  DEPLOYED_BASE_URL,
  STACK_PORT,
  STACK_URL,
  WEB_ORIGIN,
  WEB_PORT,
} from "./e2e/support/env";

/**
 * Locally and in CI: the Python stack (test Postgres, fake GitHub, `hub serve` from this checkout) and the web's
 * production build, on one origin through the /v1 rewrite. With PLAYWRIGHT_BASE_URL set, only the @deployed specs
 * run, against that hub, signed in with the storage state in EVO_E2E_STORAGE_STATE.
 */
const python = process.env.PYTHON ?? "python3";
const reuse = process.env.E2E_REUSE_SERVERS === "1";
const chrome = { ...devices["Desktop Chrome"], locale: "vi-VN", timezoneId: "Asia/Ho_Chi_Minh" };

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 4,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    reducedMotion: "reduce", // menus and sheets open without animating, so clicks and axe see settled UI
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: DEPLOYED_BASE_URL
    ? [
        {
          name: "deployed",
          grep: /@deployed/,
          use: { ...chrome, storageState: process.env.EVO_E2E_STORAGE_STATE },
        },
      ]
    : [{ name: "chromium", use: chrome }],
  webServer: DEPLOYED_BASE_URL
    ? undefined
    : [
        {
          command: `${python} e2e/hub_stack.py`,
          url: `${STACK_URL}/health`,
          env: {
            E2E_API_PORT: String(API_PORT),
            E2E_STACK_PORT: String(STACK_PORT),
            E2E_WEB_ORIGIN: WEB_ORIGIN,
            E2E_ADMIN_LOGIN: ADMIN_LOGIN,
          },
          timeout: 120_000,
          gracefulShutdown: { signal: "SIGTERM", timeout: 30_000 },
          reuseExistingServer: reuse,
          stdout: "pipe",
        },
        {
          command: "pnpm build && pnpm start",
          url: `${WEB_ORIGIN}/login`,
          env: {
            EVO_HUB_API_INTERNAL_URL: API_URL,
            PORT: String(WEB_PORT),
            HOSTNAME: "localhost",
            NEXT_TELEMETRY_DISABLED: "1",
          },
          timeout: 300_000,
          gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
          reuseExistingServer: reuse,
        },
      ],
});
