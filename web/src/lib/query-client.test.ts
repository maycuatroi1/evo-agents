import { describe, expect, it } from "vitest";

import { ApiError } from "./api/errors";
import { shouldRetry } from "./query-client";

const error = (status: number) => new ApiError({ status, code: "x", message: "x", requestId: null });

describe("shouldRetry", () => {
  it("retries a missing answer or a 5xx at most twice, and never a 4xx", () => {
    expect(shouldRetry(0, error(0))).toBe(true);
    expect(shouldRetry(1, error(503))).toBe(true);
    expect(shouldRetry(2, error(503))).toBe(false);
    for (const status of [400, 401, 403, 404, 422]) expect(shouldRetry(0, error(status))).toBe(false);
    expect(shouldRetry(0, new Error("bug"))).toBe(false);
  });
});
