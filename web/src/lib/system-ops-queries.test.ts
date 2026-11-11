import { describe, expect, it } from "vitest";

import { opsAlert, opsFormOf, opsProblems, type SystemOps, withOps } from "./system-ops-queries";
import { newSpec } from "./systems";

const ops = (over: Partial<SystemOps> = {}): SystemOps => ({
  project: "northwind",
  slug: "orchestrator",
  default: true,
  ops: { enabled: true, debounce_minutes: 5, max_items: 20 },
  ops_daily_usd: 5,
  paused: false,
  paused_at: null,
  paused_reason: null,
  spent_usd: 0,
  batches: {},
  ...over,
});

describe("the ops of a system", () => {
  it("says paused or capped only for the default system with ops on", () => {
    expect(opsAlert(ops())).toBeNull();
    expect(opsAlert(ops({ paused: true, paused_at: "2026-11-12T03:00:00Z" }))).toBe("paused");
    expect(opsAlert(ops({ spent_usd: 5 }))).toBe("capped");
    expect(opsAlert(ops({ spent_usd: 4.99 }))).toBeNull();
    expect(opsAlert(ops({ paused: true, spent_usd: 9 }))).toBe("paused");
    expect(opsAlert(ops({ default: false, paused: true }))).toBeNull();
    expect(opsAlert(ops({ ops: { enabled: false, debounce_minutes: 5, max_items: 20 }, spent_usd: 9 }))).toBeNull();
  });

  it("refuses values out of the hub's bounds, and fractions where it wants whole numbers", () => {
    const form = opsFormOf(ops());
    expect(opsProblems(form)).toEqual([]);
    expect(opsProblems({ ...form, debounce_minutes: "1441", max_items: "2.5", ops_daily_usd: "0" })).toEqual([
      "debounce_minutes",
      "max_items",
      "ops_daily_usd",
    ]);
    expect(opsProblems({ ...form, debounce_minutes: "0", max_items: "1000", ops_daily_usd: "0.01" })).toEqual([]);
    expect(opsProblems({ ...form, max_items: " " })).toEqual(["max_items"]);
  });

  it("writes the form into the spec's ops and its limits, keeping the other limits", () => {
    const spec = { ...newSpec("Support"), limits: { daily_usd: 20 } };
    const next = withOps(spec, { enabled: false, debounce_minutes: "15", max_items: "40", ops_daily_usd: "2.5" });
    expect(next.ops).toEqual({ enabled: false, debounce_minutes: 15, max_items: 40 });
    expect(next.limits).toEqual({ daily_usd: 20, ops_daily_usd: 2.5 });
    expect(next.nodes).toBe(spec.nodes);
  });
});
