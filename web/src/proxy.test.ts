import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { config, proxy, RUN_STREAM } from "./proxy";

const SESSION = `evs_${"b".repeat(43)}`;

function request(path: string, cookie?: string, headers: Record<string, string> = {}) {
  return new NextRequest(new URL(path, "http://localhost:3000"), {
    headers: { ...(cookie ? { cookie: `evo_hub_session=${cookie}` } : {}), ...headers },
  });
}

describe("proxy", () => {
  it("sends a visitor without a session to the sign-in page, with the security headers", () => {
    for (const path of ["/", "/admin", "/p/demo", "/whatever/else"]) {
      const response = proxy(request(path));
      expect(response.status, path).toBe(307);
      expect(new URL(response.headers.get("location") ?? "").pathname).toBe("/login");
      expect(response.headers.get("content-security-policy")).toContain("frame-ancestors 'none'");
      expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    }
  });

  it("treats a malformed session cookie as none", () => {
    expect(proxy(request("/", "evs_short")).status).toBe(307);
  });

  it("lets the sign-in page through without a session", () => {
    const response = proxy(request("/login"));
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });

  it("passes a request with a session on, with a fresh nonce for Next.js and the browser", () => {
    const first = proxy(request("/p/demo", SESSION));
    const second = proxy(request("/p/demo", SESSION));
    expect(first.headers.get("location")).toBeNull();
    const csp = first.headers.get("content-security-policy") ?? "";
    const nonce = /'nonce-([^']+)'/.exec(csp)?.[1];
    expect(nonce).toBeTruthy();
    expect(first.headers.get("x-middleware-request-x-nonce")).toBe(nonce);
    expect(first.headers.get("x-middleware-request-content-security-policy")).toBe(csp);
    expect(second.headers.get("content-security-policy")).not.toBe(csp);
    expect(first.headers.get("strict-transport-security")).toMatch(/^max-age=/);
  });

  it("asks for https upgrades only behind https", () => {
    expect(proxy(request("/login")).headers.get("content-security-policy")).not.toContain("upgrade-insecure-requests");
    const https = proxy(request("/login", undefined, { "x-forwarded-proto": "https" }));
    expect(https.headers.get("content-security-policy")).toContain("upgrade-insecure-requests");
  });

  it("asks for a run's event stream unencoded, with no sign-in redirect and nothing else changed", () => {
    const response = proxy(request("/v1/projects/demo/runs/12/stream", undefined, { "accept-encoding": "gzip, br", authorization: "Bearer x" }));
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("x-middleware-next")).toBe("1");
    expect(response.headers.get("x-middleware-request-accept-encoding")).toBe("identity");
    expect(response.headers.get("x-middleware-request-authorization")).toBe("Bearer x");
    expect(response.headers.get("content-security-policy")).toBeNull();
    expect(RUN_STREAM.test("/v1/projects/demo/runs/12/stream")).toBe(true);
    for (const path of ["/v1/projects/demo/runs/12/events", "/v1/projects/demo/runs/x/stream", "/v1/projects/demo/runs/12/stream/more"]) {
      expect(RUN_STREAM.test(path), path).toBe(false);
    }
    expect(config.matcher).toContain("/v1/projects/:project/runs/:id/stream");
  });

  it("does not run on the API or on build assets", () => {
    const pages = config.matcher[0];
    if (typeof pages === "string") throw new Error("the first matcher is the pages' one");
    const source = new RegExp(`^${pages.source}$`);
    for (const path of ["/", "/login", "/admin", "/p/demo"]) expect(source.test(path), path).toBe(true);
    for (const path of ["/v1/projects", "/mcp", "/_next/static/chunk.js", "/icon.svg"]) {
      expect(source.test(path), path).toBe(false);
    }
  });
});
