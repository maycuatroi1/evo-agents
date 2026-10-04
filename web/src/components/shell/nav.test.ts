import { describe, expect, it } from "vitest";

import { isActive, projectHref } from "./nav";

describe("nav", () => {
  it("builds project links", () => {
    expect(projectHref("demo")).toBe("/p/demo");
    expect(projectHref("demo", "plans")).toBe("/p/demo/plans");
  });

  it("marks the current page, and a section for its subpages", () => {
    expect(isActive("/", "/", false)).toBe(true);
    expect(isActive("/admin", "/", false)).toBe(false);
    expect(isActive("/admin/users", "/admin", false)).toBe(true);
    expect(isActive("/administrator", "/admin", false)).toBe(false);
    expect(isActive("/p/demo/plans", "/p/demo", true)).toBe(false);
    expect(isActive("/p/demo", "/p/demo", true)).toBe(true);
  });
});
