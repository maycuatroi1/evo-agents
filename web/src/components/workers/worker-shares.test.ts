import { describe, expect, it } from "vitest";

import { doctorVerdict } from "./worker-shares";

const NOW = Date.parse("2026-10-22T12:00:00Z");

describe("doctorVerdict", () => {
  it("decides as the hub does: no report, a report older than 48 hours, a high finding, or yes", () => {
    expect(doctorVerdict({ doctor_at: null, doctor_high: null }, NOW)).toEqual({ state: "none" });
    expect(doctorVerdict({ doctor_at: "2026-10-20T11:00:00Z", doctor_high: 0 }, NOW)).toEqual({ state: "stale", at: "2026-10-20T11:00:00Z" });
    expect(doctorVerdict({ doctor_at: "2026-10-22T11:00:00Z", doctor_high: 2 }, NOW)).toEqual({ state: "high", count: 2 });
    expect(doctorVerdict({ doctor_at: "2026-10-22T11:00:00Z", doctor_high: 0 }, NOW)).toEqual({ state: "ok" });
  });

  it("does not call a report old before the browser's clock is read", () => {
    expect(doctorVerdict({ doctor_at: "2020-01-01T00:00:00Z", doctor_high: 0 }, null)).toEqual({ state: "ok" });
  });
});
