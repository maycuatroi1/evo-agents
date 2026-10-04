/**
 * Names shared with the hub API. The API owns sign-in and the session cookie; the web only needs to know the
 * cookie's name (to send people without one to the sign-in page) and where the sign-in starts.
 */

/** `evo_agents.hub.server.security.SESSION_COOKIE`: httpOnly, set by GET /v1/auth/web/callback. */
export const SESSION_COOKIE = "evo_hub_session";

/** A web session is `evs_` and 32 random bytes in base64url, as `security._TOKEN` accepts it. */
export const SESSION_TOKEN = /^evs_[A-Za-z0-9_-]{43}$/;

/** Starts GitHub's web flow on the API; the callback redirects to `/` with the session cookie set. */
export const API_LOGIN_PATH = "/v1/auth/web/login";

export const LOGIN_PATH = "/login";

/** Pages reachable without a session. Everything else sends a visitor without a session to LOGIN_PATH. */
export const PUBLIC_PATHS: ReadonlySet<string> = new Set([LOGIN_PATH]);

/** The UI language preference; read by src/i18n/request.ts. */
export const LOCALE_COOKIE = "NEXT_LOCALE";

export function hasSession(value: string | undefined): boolean {
  return value !== undefined && SESSION_TOKEN.test(value);
}
