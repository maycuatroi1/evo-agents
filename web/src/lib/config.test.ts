import { describe, expect, it } from "vitest";

import { hasSession } from "./config";

describe("hasSession", () => {
  it("accepts only the shape of a web session token", () => {
    expect(hasSession(`evs_${"a".repeat(43)}`)).toBe(true);
    expect(hasSession(undefined)).toBe(false);
    expect(hasSession("")).toBe(false);
    expect(hasSession(`evh_${"a".repeat(43)}`)).toBe(false); // a machine token never comes as the cookie
    expect(hasSession(`evs_${"a".repeat(42)}`)).toBe(false);
    expect(hasSession(`evs_${"a".repeat(42)};`)).toBe(false);
  });
});
