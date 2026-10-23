import { describe, expect, it } from "vitest";

import { BARE_BROWSER, browserFields, NO_BROWSER } from "./browser-field";

describe("browserFields", () => {
  it("sends nothing for no browser, a bare browser for the Chromium alone, and the identity's name otherwise", () => {
    expect(browserFields(NO_BROWSER)).toEqual({});
    expect(browserFields(BARE_BROWSER)).toEqual({ browser: true });
    expect(browserFields("northwind")).toEqual({ identities: ["northwind"] });
  });
});
