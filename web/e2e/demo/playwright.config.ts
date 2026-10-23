import { DEMO_TIME_ZONE } from "./env"; // first: it sets the ports and the admin that ../support/env reads

import { defineConfig, devices } from "@playwright/test";

import { ADMIN_LOGIN, API_PORT, API_URL, STACK_PORT, STACK_URL, WEB_ORIGIN, WEB_PORT } from "../support/env";

/**
 * `pnpm screenshots` (scripts/screenshots.mjs) runs this config: a stack of its own, as the e2e config starts it (the
 * Python stack on a database of its own, the web's production build), never one already running, since the demo
 * seeds its people, project and runs once into an empty hub. One worker runs the one spec, `screenshots.shots.ts`,
 * which seeds the demo through the API and writes the images. Pages render in English, in UTC, without motion.
 */
const python = process.env.PYTHON ?? "python3";

export default defineConfig({
  testDir: ".",
  testMatch: /\.shots\.ts$/,
  outputDir: "../../test-results/demo",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 600_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: WEB_ORIGIN,
    locale: "en-US",
    timezoneId: DEMO_TIME_ZONE,
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `${python} e2e/hub_stack.py`,
      cwd: "../..",
      url: `${STACK_URL}/health`,
      env: {
        E2E_API_PORT: String(API_PORT),
        E2E_STACK_PORT: String(STACK_PORT),
        E2E_WEB_ORIGIN: WEB_ORIGIN,
        E2E_ADMIN_LOGIN: ADMIN_LOGIN,
      },
      timeout: 120_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 30_000 },
      reuseExistingServer: false,
      stdout: "pipe",
    },
    {
      command: "pnpm build && pnpm start",
      cwd: "../..",
      url: `${WEB_ORIGIN}/login`,
      env: {
        EVO_HUB_API_INTERNAL_URL: API_URL,
        EVO_HUB_WEB_TIME_ZONE: DEMO_TIME_ZONE,
        PORT: String(WEB_PORT),
        HOSTNAME: "localhost",
        NEXT_TELEMETRY_DISABLED: "1",
      },
      timeout: 300_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
      reuseExistingServer: false,
    },
  ],
});
