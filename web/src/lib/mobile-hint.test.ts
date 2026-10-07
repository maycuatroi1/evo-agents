import { describe, expect, it } from "vitest";

import { mobileHint } from "./mobile-hint";

const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const ANDROID =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36";
const IPAD =
  "Mozilla/5.0 (iPad; CPU OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const DESKTOP =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";

function headers(values: Record<string, string>) {
  return new Headers(values);
}

describe("mobileHint", () => {
  it("guesses a phone from its user agent", () => {
    expect(mobileHint(headers({ "user-agent": IPHONE }))).toBe(true);
    expect(mobileHint(headers({ "user-agent": ANDROID }))).toBe(true);
  });

  it("takes Chromium's client hint for a phone", () => {
    expect(mobileHint(headers({ "user-agent": DESKTOP, "sec-ch-ua-mobile": "?1" }))).toBe(true);
  });

  it("leaves tablets, desktops and requests without a user agent to the table layout", () => {
    expect(mobileHint(headers({ "user-agent": IPAD }))).toBe(false);
    expect(mobileHint(headers({ "user-agent": DESKTOP, "sec-ch-ua-mobile": "?0" }))).toBe(false);
    expect(mobileHint(headers({}))).toBe(false);
  });
});
