import { describe, expect, it } from "vitest";

import { call, createApiClient } from "./client";
import { ApiError } from "./errors";

function clientAnswering(answer: (request: Request) => Response | Promise<Response>) {
  const seen: Request[] = [];
  const api = createApiClient({
    baseUrl: "http://api.test",
    headers: { cookie: "evo_hub_session=evs_x" },
    fetch: async (request) => {
      seen.push(request);
      return answer(request);
    },
  });
  return { api, seen };
}

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

describe("call", () => {
  it("returns the data of a 2xx answer and sends the configured headers", async () => {
    const { api, seen } = clientAnswering(() => json([{ name: "demo" }]));
    const projects = await call(api.GET("/v1/projects"));
    expect(projects).toEqual([{ name: "demo" }]);
    expect(seen[0].url).toBe("http://api.test/v1/projects");
    expect(seen[0].headers.get("cookie")).toBe("evo_hub_session=evs_x");
  });

  it("throws an ApiError with the API's code and request id", async () => {
    const { api } = clientAnswering(() =>
      json({ error: "forbidden", message: "this needs a hub admin", request_id: "rid-9" }, 403, { "x-request-id": "rid-9" }),
    );
    const error = await call(api.GET("/v1/admin/stats")).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).info).toMatchObject({ status: 403, code: "forbidden", requestId: "rid-9" });
  });

  it("throws a network ApiError when nothing answers", async () => {
    const { api } = clientAnswering(() => {
      throw new TypeError("fetch failed");
    });
    const error = await call(api.GET("/v1/auth/whoami")).catch((caught: unknown) => caught);
    expect((error as ApiError).info).toMatchObject({ status: 0, code: "network" });
  });

  it("accepts a 204 without a body", async () => {
    const { api } = clientAnswering(() => new Response(null, { status: 204 }));
    await expect(call(api.POST("/v1/auth/web/logout", { headers: { "X-Evo-CSRF": "t" } }))).resolves.toBeUndefined();
  });
});
