/**
 * Security headers of every response from the web (`/` and below; `/v1` and `/mcp` belong to the API).
 *
 * Scripts run only with the per-request nonce the proxy puts in the policy ('strict-dynamic' lets the scripts
 * Next.js loads with that nonce load their chunks). 'wasm-unsafe-eval' lets them compile WebAssembly, which the run
 * page's terminal needs (libghostty's VT core, served from the web's own origin); it allows no JavaScript eval.
 * connect-src 'self' covers the terminal's websocket too: CSP Level 3 matches ws: and wss: of the page's own host
 * and port under 'self' (Chromium and Firefox; e2e/terminal.spec.ts checks both).
 *
 * Styles allow 'unsafe-inline' because React renders `style` attributes (the sidebar's width variables, Radix
 * positioning) and a nonce cannot cover attributes; no untrusted markup is ever rendered as HTML, so the remaining
 * risk is style injection only.
 */

export type Header = { key: string; value: string };

export const STATIC_SECURITY_HEADERS: readonly Header[] = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
];

export type CspOptions = {
  nonce: string;
  /** `next dev` needs eval for React's debugging and a websocket for hot reload. */
  dev?: boolean;
  /** Served over https: ask the browser to upgrade any http subresource. */
  https?: boolean;
};

export function contentSecurityPolicy({ nonce, dev = false, https = false }: CspOptions): string {
  const directives: [string, ...string[]][] = [
    ["default-src", "'self'"],
    ["script-src", "'self'", `'nonce-${nonce}'`, "'strict-dynamic'", "'wasm-unsafe-eval'", ...(dev ? ["'unsafe-eval'"] : [])],
    ["style-src", "'self'", "'unsafe-inline'"],
    ["img-src", "'self'", "data:", "blob:"],
    ["font-src", "'self'"],
    ["connect-src", "'self'", ...(dev ? ["ws:", "wss:"] : [])],
    ["object-src", "'none'"],
    ["base-uri", "'self'"],
    ["form-action", "'self'"],
    ["frame-ancestors", "'none'"],
    ["frame-src", "'none'"],
    ["worker-src", "'self'", "blob:"],
    ["manifest-src", "'self'"],
  ];
  if (https) directives.push(["upgrade-insecure-requests"]);
  return directives.map((parts) => parts.join(" ")).join("; ");
}

/** 16 random bytes in base64: unguessable and unique per request. */
export function createNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
