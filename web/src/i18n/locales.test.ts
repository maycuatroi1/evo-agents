import { describe, expect, it } from "vitest";

import { requestLocale } from "./locales";

describe("requestLocale", () => {
  it("takes the cookie first, then the hub's default, then English", () => {
    expect(requestLocale("vi", undefined)).toBe("vi");
    expect(requestLocale("en", "vi")).toBe("en");
    expect(requestLocale(undefined, "vi")).toBe("vi");
    expect(requestLocale(undefined, " VI ")).toBe("vi");
    expect(requestLocale(undefined, undefined)).toBe("en");
  });

  it("reads a value that is not a locale as none", () => {
    expect(requestLocale("fr", "vi")).toBe("vi");
    expect(requestLocale(undefined, "fr")).toBe("en");
    expect(requestLocale("", "")).toBe("en");
  });
});
