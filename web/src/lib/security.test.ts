import { describe, expect, it } from "vitest";

import { contentSecurityPolicy, createNonce, STATIC_SECURITY_HEADERS } from "./security";

function directives(policy: string): Map<string, string[]> {
  return new Map(
    policy.split("; ").map((part) => {
      const [name, ...values] = part.split(" ");
      return [name, values];
    }),
  );
}

describe("contentSecurityPolicy", () => {
  it("allows scripts only with the nonce and never lets the page be framed", () => {
    const policy = directives(contentSecurityPolicy({ nonce: "abc123" }));
    expect(policy.get("script-src")).toEqual(["'self'", "'nonce-abc123'", "'strict-dynamic'", "'wasm-unsafe-eval'"]);
    expect(policy.get("frame-ancestors")).toEqual(["'none'"]);
    expect(policy.get("object-src")).toEqual(["'none'"]);
    expect(policy.get("base-uri")).toEqual(["'self'"]);
    expect(policy.get("form-action")).toEqual(["'self'"]);
    expect(policy.get("connect-src")).toEqual(["'self'"]);
    expect(policy.has("upgrade-insecure-requests")).toBe(false);
  });

  it("adds eval and websockets only for next dev, and the upgrade only over https", () => {
    expect(contentSecurityPolicy({ nonce: "n" })).not.toContain("'unsafe-eval'");
    expect(directives(contentSecurityPolicy({ nonce: "n" })).get("connect-src")).not.toContain("ws:");
    const dev = directives(contentSecurityPolicy({ nonce: "n", dev: true }));
    expect(dev.get("script-src")).toContain("'unsafe-eval'");
    expect(dev.get("connect-src")).toContain("ws:");
    expect(directives(contentSecurityPolicy({ nonce: "n", https: true })).has("upgrade-insecure-requests")).toBe(true);
  });
});

describe("createNonce", () => {
  it("is 16 random bytes in base64, different every time", () => {
    const nonces = new Set(Array.from({ length: 50 }, createNonce));
    expect(nonces.size).toBe(50);
    for (const nonce of nonces) expect(atob(nonce)).toHaveLength(16);
  });
});

describe("STATIC_SECURITY_HEADERS", () => {
  it("covers sniffing, referrers, HSTS and framing", () => {
    const headers = Object.fromEntries(STATIC_SECURITY_HEADERS.map(({ key, value }) => [key, value]));
    expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    expect(headers["Referrer-Policy"]).toBe("same-origin");
    expect(headers["Strict-Transport-Security"]).toMatch(/^max-age=\d+/);
    expect(headers["X-Frame-Options"]).toBe("DENY");
  });
});
