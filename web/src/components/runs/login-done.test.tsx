import { describe, expect, it } from "vitest";

import { openLogin } from "./login-done";

describe("openLogin", () => {
  it("finds the open login decision and ignores the rest", () => {
    const list = [
      { id: 1, category: "scope", state: "open" },
      { id: 2, category: "login", state: "answered" },
      { id: 3, category: "login", state: "open" },
    ] as const;
    expect(openLogin(list)?.id).toBe(3);
  });

  it("is null when the run asked no login", () => {
    expect(openLogin([{ category: "scope", state: "open" } as const])).toBeNull();
    expect(openLogin(undefined)).toBeNull();
  });
});
