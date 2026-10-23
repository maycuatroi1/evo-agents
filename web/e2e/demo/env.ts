/**
 * The demo stack's settings, set before e2e/support/env.ts reads them: import this module first in every module of
 * the demo that Playwright loads (its config and its spec). The ports differ from the e2e defaults, so `pnpm
 * screenshots` can run while `pnpm exec playwright test` holds 3324; the hub admin is the demo's Maya Lindqvist, whom
 * the images show signed in. Dates render in UTC, on the server (EVO_HUB_WEB_TIME_ZONE) and in the browser alike.
 */
export const DEMO_ADMIN_LOGIN = "maya-lindqvist";
export const DEMO_TIME_ZONE = "UTC";

process.env.E2E_WEB_PORT ??= "3344";
process.env.E2E_API_PORT ??= "18344";
process.env.E2E_STACK_PORT ??= "18345";
process.env.E2E_ADMIN_LOGIN = DEMO_ADMIN_LOGIN;
