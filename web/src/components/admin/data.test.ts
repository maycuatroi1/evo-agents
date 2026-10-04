import { describe, expect, it } from "vitest";

import { createApiClient } from "@/lib/api/client";

import { adminCrumbs } from "./crumbs";
import {
  type AdminUser,
  auditHref,
  auditParams,
  deleteGrant,
  filterMembers,
  fromRecord,
  nextDay,
  parseAuditFilters,
  parseMemberFilters,
  parseTokenFilters,
  putGrant,
  revokeToken,
  startOfDay,
  tokenParams,
  tokensHref,
} from "./data";

const params = (text: string) => new URLSearchParams(text);

describe("audit filters from the URL", () => {
  it("keeps valid values and drops the rest", () => {
    expect(parseAuditFilters(params("actor=Octo&action=grant.put&project=demo&from=2026-10-01&to=2026-10-04&limit=25&cursor=abc_-1"))).toEqual({
      actor: "Octo",
      action: "grant.put",
      project: "demo",
      from: "2026-10-01",
      to: "2026-10-04",
      limit: 25,
      cursor: "abc_-1",
    });
    expect(parseAuditFilters(params("actor=-x&action=Grant&project=Demo&from=2026-02-30&to=nope&limit=7&cursor=a%20b"))).toEqual({
      actor: "",
      action: "",
      project: "",
      from: "",
      to: "",
      limit: 50,
      cursor: "",
    });
  });

  it("puts a reversed date range the right way round", () => {
    const filters = parseAuditFilters(params("from=2026-10-04&to=2026-10-01"));
    expect([filters.from, filters.to]).toEqual(["2026-10-01", "2026-10-04"]);
  });

  it("reads a server page's searchParams record the same way", () => {
    const record = { actor: ["octo", "other"], limit: "100", ignored: undefined };
    expect(parseAuditFilters(fromRecord(record))).toEqual(parseAuditFilters(params("actor=octo&limit=100")));
  });

  it("asks the API for whole days in the hub's time zone, the last day included", () => {
    const filters = parseAuditFilters(params("actor=octo&from=2026-10-01&to=2026-10-04"));
    expect(auditParams(filters, "Asia/Ho_Chi_Minh")).toEqual({
      actor: "octo",
      since: "2026-09-30T17:00:00.000Z",
      until: "2026-10-04T17:00:00.000Z",
      limit: 50,
    });
    expect(auditParams(parseAuditFilters(params("")), "UTC")).toEqual({ limit: 50 });
  });

  it("leaves defaults out of the URL", () => {
    expect(auditHref({ actor: "octo", limit: 50, cursor: "" })).toBe("/admin/audit?actor=octo");
    expect(auditHref({ limit: 25 })).toBe("/admin/audit?limit=25");
  });
});

describe("days in a time zone", () => {
  it("finds midnight across the date line and across a DST change", () => {
    expect(startOfDay("2026-10-04", "UTC")).toBe("2026-10-04T00:00:00.000Z");
    expect(startOfDay("2026-10-04", "Asia/Ho_Chi_Minh")).toBe("2026-10-03T17:00:00.000Z");
    expect(startOfDay("2026-10-04", "America/Los_Angeles")).toBe("2026-10-04T07:00:00.000Z"); // PDT
    expect(startOfDay("2026-12-04", "America/Los_Angeles")).toBe("2026-12-04T08:00:00.000Z"); // PST
    expect(startOfDay("2026-03-29", "Europe/Berlin")).toBe("2026-03-28T23:00:00.000Z"); // the day clocks go forward
  });

  it("steps to the next calendar day across month and year ends", () => {
    expect(nextDay("2026-10-31")).toBe("2026-11-01");
    expect(nextDay("2026-12-31")).toBe("2027-01-01");
    expect(nextDay("2028-02-28")).toBe("2028-02-29");
  });
});

describe("token filters", () => {
  it("defaults to live tokens of everyone", () => {
    expect(tokenParams(parseTokenFilters(params("")))).toEqual({ state: "active", limit: 50 });
    expect(tokenParams(parseTokenFilters(params("login=octo&kind=web&state=any&limit=100&cursor=c1")))).toEqual({
      login: "octo",
      kind: "web",
      state: "any",
      limit: 100,
      cursor: "c1",
    });
    expect(parseTokenFilters(params("kind=robot&state=gone"))).toMatchObject({ kind: "", state: "active" });
  });

  it("leaves defaults out of the URL", () => {
    expect(tokensHref({ login: "octo" })).toBe("/admin/tokens?login=octo");
    expect(tokensHref({ login: "octo", state: "any" })).toBe("/admin/tokens?login=octo&state=any");
  });
});

describe("members", () => {
  const user = (login: string, projects: string[]): AdminUser => ({
    login,
    admin: false,
    signed_in: true,
    created_at: "2026-10-01T00:00:00Z",
    last_seen_at: null,
    active_tokens: 0,
    grants: projects.map((project) => ({
      project,
      role: "reader",
      max_level: "internal",
      granted_by: "admin",
      granted_at: "2026-10-01T00:00:00Z",
    })),
  });
  const users = [user("Octo", ["demo"]), user("hubot", ["other"]), user("mona", [])];

  it("filters by a piece of the login, any case, and by project", () => {
    expect(filterMembers(users, { q: "OC", project: "" }).map((u) => u.login)).toEqual(["Octo"]);
    expect(filterMembers(users, { q: "", project: "other" }).map((u) => u.login)).toEqual(["hubot"]);
    expect(filterMembers(users, { q: "o", project: "demo" }).map((u) => u.login)).toEqual(["Octo"]);
    expect(filterMembers(users, parseMemberFilters(params("project=Bad Name")))).toHaveLength(3);
  });
});

describe("breadcrumbs", () => {
  const label = (section: string) => `[${section}]`;

  it("names the area, the section and the member", () => {
    expect(adminCrumbs([], "Admin", label)).toEqual([{ label: "Admin" }]);
    expect(adminCrumbs(["audit"], "Admin", label)).toEqual([{ label: "Admin", href: "/admin" }, { label: "[audit]" }]);
    expect(adminCrumbs(["members", "octo%2Dcat"], "Admin", label)).toEqual([
      { label: "Admin", href: "/admin" },
      { label: "[members]", href: "/admin/members" },
      { label: "octo-cat" },
    ]);
  });
});

describe("admin writes", () => {
  function recordingApi(answer: (request: Request) => Response) {
    const seen: Request[] = [];
    const api = createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        seen.push(request);
        if (request.url.endsWith("/v1/auth/web/csrf"))
          return Response.json({ csrf: "csrf-of-this-session", header: "X-Evo-CSRF" });
        return answer(request);
      },
    });
    return { api, seen };
  }

  it("fetches the session's CSRF token before each write and sends it", async () => {
    const { api, seen } = recordingApi((request) =>
      request.method === "PUT"
        ? Response.json({ project: "demo", login: "octo", role: "reader", max_level: "internal", created: true })
        : new Response(null, { status: 204 }),
    );
    await putGrant(api, { login: "octo", project: "demo", role: "reader", maxLevel: "internal" });
    await deleteGrant(api, "demo", "octo");
    await revokeToken(api, 12);
    const writes = seen.filter((request) => request.method !== "GET");
    expect(writes.map((request) => `${request.method} ${new URL(request.url).pathname}`)).toEqual([
      "PUT /v1/admin/projects/demo/grants/octo",
      "DELETE /v1/admin/projects/demo/grants/octo",
      "DELETE /v1/admin/tokens/12",
    ]);
    for (const request of writes) expect(request.headers.get("X-Evo-CSRF")).toBe("csrf-of-this-session");
    expect(await writes[0].json()).toEqual({ role: "reader", max_level: "internal" });
    expect(seen.filter((request) => request.method === "GET")).toHaveLength(3);
  });

  it("surfaces the API's refusal as an ApiError", async () => {
    const { api } = recordingApi(() =>
      Response.json({ error: "conflict", message: "token 12 was revoked already" }, { status: 409 }),
    );
    await expect(revokeToken(api, 12)).rejects.toMatchObject({ info: { status: 409, code: "conflict" } });
  });
});
