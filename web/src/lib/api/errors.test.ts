import { describe, expect, it } from "vitest";

import { ApiError, errorKind, fromResponse, networkError, toInfo } from "./errors";

describe("fromResponse", () => {
  it("reads the hub's JSON error body", () => {
    const response = new Response(null, { status: 403, headers: { "x-request-id": "from-header" } });
    const error = fromResponse(response, { error: "forbidden", message: "this needs a hub admin", request_id: "rid-1" });
    expect(error.info).toEqual({
      status: 403,
      code: "forbidden",
      message: "this needs a hub admin",
      requestId: "rid-1",
    });
    expect(error.kind).toBe("forbidden");
  });

  it("keeps what the hub said beside its message, and leaves it out when it said nothing more", () => {
    const conflict = { item: "k1", code: "UC ORD-1", base: "old", latest: "theirs" };
    const response = new Response(null, { status: 409 });
    const error = fromResponse(response, { error: "conflict", message: "items changed", request_id: "rid-3", detail: [conflict] });
    expect(error.info.detail).toEqual([conflict]);
    expect(toInfo(error).detail).toEqual([conflict]);
    expect("detail" in fromResponse(response, { error: "conflict", message: "x", detail: [] }).info).toBe(false);
    expect("detail" in fromResponse(response, { error: "conflict", message: "x", detail: "text" }).info).toBe(false);
  });

  it("falls back to the status and the X-Request-ID header without a body", () => {
    const response = new Response(null, { status: 502, statusText: "Bad Gateway", headers: { "x-request-id": "rid-2" } });
    expect(fromResponse(response, undefined).info).toEqual({
      status: 502,
      code: "http_502",
      message: "Bad Gateway",
      requestId: "rid-2",
    });
  });
});

describe("errorKind", () => {
  it.each([
    [0, "network"],
    [401, "unauthorized"],
    [403, "forbidden"],
    [404, "not_found"],
    [409, "client"],
    [422, "client"],
    [500, "server"],
    [503, "server"],
  ] as const)("%i is %s", (status, kind) => {
    expect(errorKind(status)).toBe(kind);
  });
});

describe("networkError and toInfo", () => {
  it("turns anything thrown before an answer into status 0", () => {
    expect(networkError(new TypeError("fetch failed")).info).toEqual({
      status: 0,
      code: "network",
      message: "fetch failed",
      requestId: null,
    });
    expect(toInfo("boom").status).toBe(0);
    const info = toInfo(new ApiError({ status: 404, code: "not_found", message: "no", requestId: null }));
    expect(info).toEqual({ status: 404, code: "not_found", message: "no", requestId: null });
  });
});
