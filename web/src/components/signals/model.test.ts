import { describe, expect, it } from "vitest";

import { signalsHref } from "@/lib/signal-queries";

import { latestFilter, pointsOf, queryOf, searchOf, seriesFilter, sinceOf } from "./model";

describe("the Signals page's address", () => {
  it("reads a key, a tenant and a span, and leaves out what the hub would refuse", () => {
    expect(queryOf({ key: "checkout.conversion", tenant: "northwind", days: "30" })).toEqual({ key: "checkout.conversion", tenant: "northwind", days: 30 });
    expect(queryOf({ key: "Not A Key", tenant: "x".repeat(101), days: "12" })).toEqual({ key: null, tenant: null, days: 90 });
    expect(queryOf({ key: ["checkout.conversion", "other.key"] })).toEqual({ key: "checkout.conversion", tenant: null, days: 90 });
  });

  it("writes it back without the defaults", () => {
    expect(searchOf({ key: "checkout.conversion", tenant: null, days: 90 })).toBe("?key=checkout.conversion");
    expect(searchOf({ key: "checkout.conversion", tenant: "lumen co", days: 7 })).toBe("?key=checkout.conversion&tenant=lumen+co&days=7");
    expect(searchOf({ key: null, tenant: null, days: 90 })).toBe("");
  });

  it("is the page the value check links from its evidence", () => {
    expect(signalsHref("demo", "checkout.conversion", "northwind")).toBe("/p/demo/signals?key=checkout.conversion&tenant=northwind");
    expect(signalsHref("demo")).toBe("/p/demo/signals");
  });
});

describe("the reads of a series", () => {
  it("start at midnight UTC, so the server and the browser ask the same all day", () => {
    expect(sinceOf(7, new Date("2026-10-21T15:00:00Z"))).toBe("2026-10-14T00:00:00.000Z");
    expect(seriesFilter({ key: "a.b", tenant: null, days: 30 }, new Date("2026-10-21T01:00:00Z"))).toEqual({
      key: "a.b",
      tenant: null,
      since: "2026-09-21T00:00:00.000Z",
      limit: 1000,
    });
    expect(latestFilter("a.b")).toEqual({ key: "a.b", tenant: null, since: null, limit: 1 });
  });

  it("draw the numbers oldest first and leave out feedback", () => {
    const base = { key: "a.b", kind: "number" as const, source: "svc", tenant: null, synthetic: false, external_id: null, label: {}, via: "ingest" as const, ingest_key: "svc", login: null, run_id: null, batch_id: "b", received_at: "2026-10-21T00:00:00Z" };
    const points = pointsOf([
      { ...base, id: 2, value: 0.5, text: null, measured_at: "2026-10-20T00:00:00Z" },
      { ...base, id: 1, value: 0.4, text: null, measured_at: "2026-10-19T00:00:00Z" },
      { ...base, id: 3, value: null, text: "great", measured_at: "2026-10-21T00:00:00Z" },
    ]);
    expect(points.map((point) => point.value)).toEqual([0.4, 0.5]);
  });
});
