import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { CHAT_STREAM, config, proxy, RUN_STREAM } from "./proxy";

const SESSION = `evs_${"b".repeat(43)}`;

function request(path: string, cookie?: string, headers: Record<string, string> = {}) {
  return new NextRequest(new URL(path, "http://localhost:3000"), {
    headers: { ...(cookie ? { cookie: `evo_hub_session=${cookie}` } : {}), ...headers },
  });
}

describe("proxy", () => {
  it("sends a visitor without a session to the sign-in page, with the security headers", () => {
    for (const path of ["/admin", "/p/demo", "/whatever/else", "/about/more", "/robots"]) {
      const response = proxy(request(path));
      expect(response.status, path).toBe(307);
      expect(new URL(response.headers.get("location") ?? "").pathname).toBe("/login");
      expect(response.headers.get("content-security-policy")).toContain("frame-ancestors 'none'");
      expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    }
  });

  it("lets an invite link's page through without a session, and nothing under it", () => {
    for (const path of [`/invite/evn_${"a".repeat(43)}`, "/invite/not-a-token"]) {
      const response = proxy(request(path));
      expect(response.headers.get("location"), path).toBeNull();
      expect(response.headers.get("x-middleware-next")).toBe("1");
      expect(response.headers.get("content-security-policy")).toContain("frame-ancestors 'none'");
    }
    for (const path of ["/invite", "/invite/", "/invite/a/b", "/invites/x", "/p/invite/x"]) {
      expect(proxy(request(path)).status, path).toBe(307);
    }
  });

  it("gives a visitor without a session the product page at /, under the same URL, with a nonce", () => {
    const response = proxy(request("/"));
    expect(response.headers.get("location")).toBeNull();
    expect(new URL(response.headers.get("x-middleware-rewrite") ?? "").pathname).toBe("/about");
    const csp = response.headers.get("content-security-policy") ?? "";
    expect(csp).toContain("frame-ancestors 'none'");
    expect(response.headers.get("x-middleware-request-x-nonce")).toBe(/'nonce-([^']+)'/.exec(csp)?.[1]);
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("gives a member Home at /, and the product page at its own path to anyone", () => {
    const member = proxy(request("/", SESSION));
    expect(member.headers.get("x-middleware-rewrite")).toBeNull();
    expect(member.headers.get("x-middleware-next")).toBe("1");
    for (const cookie of [undefined, SESSION]) {
      const response = proxy(request("/about", cookie));
      expect(response.headers.get("location")).toBeNull();
      expect(response.headers.get("x-middleware-rewrite")).toBeNull();
      expect(response.headers.get("x-middleware-next")).toBe("1");
    }
  });

  it("treats a malformed session cookie as none", () => {
    expect(proxy(request("/admin", "evs_short")).status).toBe(307);
    expect(proxy(request("/", "evs_short")).headers.get("x-middleware-rewrite")).toContain("/about");
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
    // Images: the web's own, and the GitHub avatars of a chat's people, nothing else.
    expect(/(?:^|; )img-src ([^;]*)/.exec(csp)?.[1]).toBe("'self' data: blob: https://avatars.githubusercontent.com");
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

  it("asks for a chat's event stream unencoded too", () => {
    const response = proxy(request("/v1/chats/7/stream", undefined, { "accept-encoding": "gzip, br", "last-event-id": "41" }));
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("x-middleware-request-accept-encoding")).toBe("identity");
    expect(response.headers.get("x-middleware-request-last-event-id")).toBe("41");
    expect(response.headers.get("content-security-policy")).toBeNull();
    expect(CHAT_STREAM.test("/v1/chats/7/stream")).toBe(true);
    for (const path of ["/v1/chats/7/events", "/v1/chats/x/stream", "/v1/chats/7/stream/more", "/v1/chats/stream"]) {
      expect(CHAT_STREAM.test(path), path).toBe(false);
    }
    expect(config.matcher).toContain("/v1/chats/:id/stream");
  });

  it("does not run on the API or on build assets", () => {
    const pages = config.matcher[0];
    if (typeof pages === "string") throw new Error("the first matcher is the pages' one");
    const source = new RegExp(`^${pages.source}$`);
    for (const path of ["/", "/about", "/login", "/admin", "/p/demo"]) expect(source.test(path), path).toBe(true);
    for (const path of [
      "/v1/projects",
      "/mcp",
      "/_next/static/chunk.js",
      "/icon.svg",
      "/icon-192.png",
      "/icon-512.png",
      "/apple-icon.png",
      "/manifest.webmanifest",
      "/robots.txt",
      "/sw.js",
      "/product/og.png",
      "/provider-logos/openai.svg",
    ]) {
      expect(source.test(path), path).toBe(false);
    }
  });

  it("leaves the service worker to a visitor without a session, and not a page named like it", () => {
    const pages = config.matcher[0];
    if (typeof pages === "string") throw new Error("the first matcher is the pages' one");
    const source = new RegExp(`^${pages.source}$`);
    // The browser fetches /sw.js again to check for an update, signed in or not: the proxy never sees it.
    expect(source.test("/sw.js")).toBe(false);
    // A page named like it is still a page, and still asks for a session.
    for (const path of ["/sw", "/sw-settings"]) {
      expect(source.test(path), path).toBe(true);
      expect(proxy(request(path)).status, path).toBe(307);
    }
  });
});
