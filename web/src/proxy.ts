import { NextResponse, type NextRequest } from "next/server";

import { hasSession, LOGIN_PATH, PUBLIC_PATHS, SESSION_COOKIE } from "@/lib/config";
import { contentSecurityPolicy, createNonce, STATIC_SECURITY_HEADERS } from "@/lib/security";

/** A run's event stream (server-sent events), the one API route this proxy sees. */
export const RUN_STREAM = /^\/v1\/projects\/[^/]+\/runs\/[0-9]+\/stream$/;

/**
 * Runs before every page: sends a visitor without a session cookie to the sign-in page, and gives each response
 * a fresh CSP nonce and the security headers. Whether the session is still valid, and what it may see, is the
 * API's decision: a page that gets a 401 from the API sends the visitor to the sign-in page itself.
 *
 * Where no reverse proxy sends /v1 to the API itself (locally, Playwright, deploy/hub/docker-compose.dev.yml), the web
 * forwards it, and Next.js gzips what it forwards: a gzip stream holds a run's server-sent events back until its
 * buffer fills. For the run stream the proxy asks for the answer as it is, so each event reaches the browser when the
 * API sends it, and leaves everything else of the request alone.
 */
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (RUN_STREAM.test(pathname)) {
    const headers = new Headers(request.headers);
    headers.set("accept-encoding", "identity");
    return NextResponse.next({ request: { headers } });
  }
  const nonce = createNonce();
  const forwardedProto = request.headers.get("x-forwarded-proto");
  const https = (forwardedProto ?? request.nextUrl.protocol.replace(":", "")) === "https";
  const csp = contentSecurityPolicy({ nonce, dev: process.env.NODE_ENV === "development", https });

  let response: NextResponse;
  if (!PUBLIC_PATHS.has(pathname) && !hasSession(request.cookies.get(SESSION_COOKIE)?.value)) {
    response = NextResponse.redirect(new URL(LOGIN_PATH, request.url));
  } else {
    const headers = new Headers(request.headers);
    headers.set("x-nonce", nonce);
    headers.set("content-security-policy", csp); // Next.js reads the nonce for its own scripts from here
    response = NextResponse.next({ request: { headers } });
  }
  response.headers.set("Content-Security-Policy", csp);
  for (const { key, value } of STATIC_SECURITY_HEADERS) response.headers.set(key, value);
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not the API (/v1, /mcp, served by FastAPI), build assets, or files from public/.
      source: "/((?!v1/|mcp|_next/static|_next/image|favicon.ico|icon.svg|robots.txt).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
    // The run stream, for its encoding only (see above).
    "/v1/projects/:project/runs/:id/stream",
  ],
};
