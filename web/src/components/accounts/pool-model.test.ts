import { describe, expect, it } from "vitest";

import { poolPolicyBody, poolPolicyFormOf, poolPolicyProblems, poolUsage } from "./pool-model";

const POOL = { strategy: "round-robin", hard_reserve_pct: 10, five_hour_ceiling_pct: 90 };

describe("a pool's policy", () => {
  it("reads the pool's strategy and thresholds, an unknown strategy as round-robin", () => {
    expect(poolPolicyFormOf(POOL)).toEqual({ strategy: "round-robin", reservePct: "10", ceilingPct: "90" });
    expect(poolPolicyFormOf({ ...POOL, strategy: "newer" }).strategy).toBe("round-robin");
  });

  it("sends only what changed, the thresholds under quota-pacing alone", () => {
    const form = poolPolicyFormOf(POOL);
    expect(poolPolicyBody(form, POOL)).toBeNull();
    expect(poolPolicyBody({ ...form, strategy: "fill-first", reservePct: "20" }, POOL)).toEqual({ strategy: "fill-first" });
    expect(poolPolicyBody({ ...form, strategy: "quota-pacing", reservePct: "20" }, POOL)).toEqual({ strategy: "quota-pacing", hard_reserve_pct: 20 });
    const pacing = { ...POOL, strategy: "quota-pacing" };
    expect(poolPolicyBody({ strategy: "quota-pacing", reservePct: "10", ceilingPct: "75" }, pacing)).toEqual({ five_hour_ceiling_pct: 75 });
  });

  it("checks the thresholds as whole percents in their bounds under quota-pacing", () => {
    const form = { strategy: "quota-pacing" as const, reservePct: "x", ceilingPct: "0" };
    expect(poolPolicyProblems(form)).toEqual({ reservePct: "whole", ceilingPct: "range" });
    expect(poolPolicyProblems({ ...form, strategy: "fill-first" })).toEqual({});
    expect(poolPolicyProblems({ ...form, reservePct: "0", ceilingPct: "100" })).toEqual({});
  });
});

describe("a pool account's usage", () => {
  it("is the usage the hub kept when it has a known status, else none", () => {
    const usage = { status: "allowed", windows: { seven_day: { utilization: 0.4, resets_at: "2026-11-20T00:00:00Z" } } };
    expect(poolUsage({ last_usage: usage })).toEqual(usage);
    expect(poolUsage({ last_usage: null })).toBeNull();
    expect(poolUsage({ last_usage: { status: "odd" } })).toBeNull();
    expect(poolUsage({ last_usage: { windows: {} } })).toBeNull();
  });
});
