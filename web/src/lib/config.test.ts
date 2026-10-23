import { describe, expect, it } from "vitest";

import { hasSession, isPublicPath, PRODUCT_PATH } from "./config";

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

describe("isPublicPath", () => {
  it("opens the sign-in page, the product page and an invite link's page, and nothing else", () => {
    for (const path of ["/login", PRODUCT_PATH, "/invite/evn_x"]) expect(isPublicPath(path), path).toBe(true);
    for (const path of ["/", "/about/", "/about/x", "/admin", "/invite"]) expect(isPublicPath(path), path).toBe(false);
  });
});
