import { describe, expect, it } from "vitest";

import { createApiClient } from "@/lib/api/client";

import { LIST_PAGES, memoryListQuery, parseMemoryId, parseRevision } from "./queries";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function memory(id: number) {
  return {
    id,
    scope: "project",
    project: "demo",
    location: "harness",
    name: `m${id}.md`,
    type: "project",
    owner: "alice",
    label: { level: "internal" },
    body: "",
    revision: 1,
    deleted: false,
    created_at: "2026-10-04T00:00:00Z",
    updated_at: "2026-10-04T00:00:00Z",
    updated_by: "alice",
  };
}

describe("memoryListQuery", () => {
  it("follows the cursor and asks for one project's memories, no sink named", async () => {
    const seen: URL[] = [];
    const api = createApiClient({
      baseUrl: "http://api.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        seen.push(url);
        const cursor = url.searchParams.get("cursor");
        return json(cursor ? { items: [memory(2)], next_cursor: null } : { items: [memory(1)], next_cursor: "c1" });
      },
    });
    const options = memoryListQuery(() => api, { kind: "project", project: "demo" });
    const result = await options.queryFn!({ signal: new AbortController().signal } as never);
    expect(result).toEqual({ items: [memory(1), memory(2)], truncated: false });
    expect(seen.map((url) => url.search)).toEqual([
      "?scope=project&project=demo&limit=500",
      "?scope=project&project=demo&limit=500&cursor=c1",
    ]);
    expect(seen.every((url) => !url.searchParams.has("sink"))).toBe(true);
  });

  it("stops after its last page and says so", async () => {
    let calls = 0;
    const api = createApiClient({
      baseUrl: "http://api.test",
      fetch: async () => {
        calls += 1;
        return json({ items: [memory(calls)], next_cursor: `c${calls}` });
      },
    });
    const options = memoryListQuery(() => api, { kind: "personal" });
    const result = await options.queryFn!({ signal: new AbortController().signal } as never);
    expect(calls).toBe(LIST_PAGES);
    expect(result.truncated).toBe(true);
  });
});

describe("ids from the URL", () => {
  it("accepts what the API accepts", () => {
    expect(parseMemoryId("42")).toBe(42);
    for (const bad of ["0", "-1", "01", "1.5", "abc", "99999999999999999"]) expect(parseMemoryId(bad), bad).toBeNull();
    expect(parseRevision("3")).toBe(3);
    for (const bad of [undefined, ["3"], "0", "x", "2147483648"]) expect(parseRevision(bad)).toBeNull();
  });
});
