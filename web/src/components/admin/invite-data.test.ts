import { describe, expect, it } from "vitest";

import { inviteLink, revocable, validDays } from "./invite-data";

describe("invite links in the admin area", () => {
  it("revoke what the API takes back: an open link, or a register right no registration used", () => {
    expect(revocable({ status: "open", kind: "join", used_at: null })).toBe(true);
    expect(revocable({ status: "accepted", kind: "join", used_at: null })).toBe(false);
    expect(revocable({ status: "accepted", kind: "register", used_at: null })).toBe(true);
    expect(revocable({ status: "accepted", kind: "register", used_at: "2026-10-20T00:00:00Z" })).toBe(false);
    expect(revocable({ status: "revoked", kind: "register", used_at: null })).toBe(false);
    expect(revocable({ status: "expired", kind: "join", used_at: null })).toBe(false);
  });

  it("take whole days from 1 to 30", () => {
    expect(["1", "7", "30", " 12 "].map(validDays)).toEqual([true, true, true, true]);
    expect(["0", "31", "2.5", "", "-1", "7d"].map(validDays)).toEqual([false, false, false, false, false, false]);
  });

  it("hand out the hub's own link, or this web's page of it without a public URL", () => {
    expect(inviteLink({ url: "https://hub.acme.dev/invite/evn_a", token: "evn_a" }, "http://localhost:3000")).toBe(
      "https://hub.acme.dev/invite/evn_a",
    );
    expect(inviteLink({ url: null, token: "evn_a" }, "http://localhost:3000")).toBe("http://localhost:3000/invite/evn_a");
  });
});
