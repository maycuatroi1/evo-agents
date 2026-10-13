import { describe, expect, it } from "vitest";

import {
  accountBody,
  accountFormOf,
  accountProblems,
  type PolicyForm,
  policyBody,
  policyFormOf,
  policyProblems,
  policyRows,
  usagePercent,
  usageResetsAt,
} from "./model";
import type { Account, AccountPolicy } from "./queries";

const ACCOUNT: Account = {
  name: "claude-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  projects: ["demo", "docs"],
  workers: [],
  weight: 2,
  priority: 1,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: null,
  state: "ready",
  last_usage: null,
  last_usage_at: null,
  runs_24h: 0,
  runs_7d: 0,
  cost_24h_usd: 0,
  cost_7d_usd: 0,
  held_by: [],
};

const POLICY: AccountPolicy = {
  project: "docs",
  strategy: "fill-first",
  cooldown_seconds: 1800,
  failover: false,
  max_failovers: 5,
  updated_at: "2026-10-12T08:00:00Z",
};

describe("an account's last usage", () => {
  it("reads the highest share of a limit in whole percent, and none before any report", () => {
    expect(usagePercent(null)).toBeNull();
    expect(usagePercent({ status: "allowed" })).toBeNull();
    expect(usagePercent({ status: "allowed_warning", utilization: 0.5, windows: { five_hour: { utilization: 0.854 } } })).toBe(85);
    expect(usagePercent({ status: "rejected", utilization: 1.4 })).toBe(100);
  });

  it("resets at the usage's own time, else the latest of its windows'", () => {
    expect(usageResetsAt({ status: "rejected", resets_at: "2026-10-13T10:00:00Z" })).toBe("2026-10-13T10:00:00Z");
    const windows = { five_hour: { resets_at: "2026-10-13T10:00:00Z" }, seven_day: { resets_at: "2026-10-18T00:00:00Z" } };
    expect(usageResetsAt({ status: "allowed", windows })).toBe("2026-10-18T00:00:00Z");
    expect(usageResetsAt({ status: "allowed" })).toBeNull();
  });
});

describe("the policies by project", () => {
  it("lists each project of the accounts and of the policies, the defaults where none is set", () => {
    const rows = policyRows([ACCOUNT], [POLICY, { ...POLICY, project: "old" }]);
    expect(rows.map((row) => [row.project, row.custom, row.strategy])).toEqual([
      ["demo", false, "round-robin"],
      ["docs", true, "fill-first"],
      ["old", true, "fill-first"],
    ]);
    expect(rows[0]).toMatchObject({ cooldown_seconds: 3600, failover: true, max_failovers: 3, updated_at: null });
  });
});

describe("the forms", () => {
  it("checks an account's weight and priority as whole numbers in their bounds", () => {
    const form = accountFormOf(ACCOUNT);
    expect(form).toEqual({ weight: "2", priority: "1" });
    expect(accountProblems(form)).toEqual({});
    expect(accountBody({ weight: " 0 ", priority: "1000" })).toEqual({ weight: 0, priority: 1000 });
    expect(accountProblems({ weight: "1.5", priority: "1001" })).toEqual({ weight: "whole", priority: "range" });
  });

  it("sends a policy's cooldown in seconds, and its failovers only as a valid number", () => {
    const form = policyFormOf(POLICY);
    expect(form).toEqual({ strategy: "fill-first", cooldownMinutes: "30", failover: false, maxFailovers: "5" });
    expect(policyBody(form)).toEqual({ strategy: "fill-first", cooldown_seconds: 1800, failover: false, max_failovers: 5 });
    const off: PolicyForm = { ...form, maxFailovers: "x" };
    expect(policyProblems(off)).toEqual({});
    expect(policyBody(off).max_failovers).toBe(3);
    expect(policyProblems({ ...off, failover: true, cooldownMinutes: "0" })).toEqual({ cooldownMinutes: "range", maxFailovers: "whole" });
    expect(policyProblems({ ...form, cooldownMinutes: "10081" })).toEqual({ cooldownMinutes: "range" });
  });
});
