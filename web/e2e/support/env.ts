/**
 * Where the e2e stack runs. Fixed ports by default (overridable), so playwright.config.ts, its workers and the
 * Python stack agree without passing values around; pick other ports when two checkouts run e2e at once.
 */
const int = (name: string, fallback: number) => Number.parseInt(process.env[name] ?? "", 10) || fallback;

export const WEB_PORT = int("E2E_WEB_PORT", 3324);
export const API_PORT = int("E2E_API_PORT", 18324);
export const STACK_PORT = int("E2E_STACK_PORT", 18325);

/** The web as the browser sees it; also the hub's EVO_HUB_PUBLIC_URL, so the OAuth callback comes back here. */
export const WEB_ORIGIN = `http://localhost:${WEB_PORT}`;
export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const STACK_URL = `http://127.0.0.1:${STACK_PORT}`;
export const ADMIN_LOGIN = process.env.E2E_ADMIN_LOGIN ?? "e2e-admin";

/** Set to run only the @deployed specs against a deployed hub, signed in with EVO_E2E_STORAGE_STATE. */
export const DEPLOYED_BASE_URL = process.env.PLAYWRIGHT_BASE_URL?.replace(/\/+$/, "") || null;
export const BASE_URL = DEPLOYED_BASE_URL ?? WEB_ORIGIN;
