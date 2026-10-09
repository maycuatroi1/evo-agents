import { describe, expect, it } from "vitest";

import { telegramView } from "./telegram";

const NOW = Date.parse("2026-10-09T06:30:00Z");
const LINK = { expires_at: "2026-10-09T06:40:00Z" };
const set = (linked: boolean, enabled: boolean, configured = true) => ({ configured, linked, enabled });

describe("telegramView", () => {
  it("says the hub has no bot before anything else", () => {
    expect(telegramView(set(false, false, false), null, NOW)).toBe("unconfigured");
    expect(telegramView(set(true, true, false), LINK, NOW)).toBe("unconfigured");
  });

  it("is linked once the hub says so, whatever link is still out", () => {
    expect(telegramView(set(true, true), null, NOW)).toBe("linked");
    expect(telegramView(set(true, true), LINK, NOW)).toBe("linked");
  });

  it("waits for a link until it expires", () => {
    expect(telegramView(set(false, false), LINK, NOW)).toBe("pending");
    expect(telegramView(set(false, false), LINK, Date.parse(LINK.expires_at))).toBe("expired");
    expect(telegramView(set(false, false), LINK, 0)).toBe("pending"); // before the clock's first tick
  });

  it("tells a chat Telegram refused from no chat at all", () => {
    expect(telegramView(set(true, false), null, NOW)).toBe("off");
    expect(telegramView(set(true, false), LINK, NOW)).toBe("pending"); // a new link turns it on again
    expect(telegramView(set(false, false), null, NOW)).toBe("unlinked");
  });
});
