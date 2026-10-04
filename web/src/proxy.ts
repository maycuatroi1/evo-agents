import { NextResponse, type NextRequest } from "next/server";

import { hasSession, LOGIN_PATH, PUBLIC_PATHS, SESSION_COOKIE } from "@/lib/config";
import { contentSecurityPolicy, createNonce, STATIC_SECURITY_HEADERS } from "@/lib/security";

/**
 * Runs before every page: sends a visitor without a session cookie to the sign-in page, and gives each response
 * a fresh CSP nonce and the security headers. Whether the session is still valid, and what it may see, is the
 * API's decision: a page that gets a 401 from the API sends the visitor to the sign-in page itself.
 */
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
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
  ],
};
