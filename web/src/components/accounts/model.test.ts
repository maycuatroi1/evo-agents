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
  renewalDue,
} from "./model";
import type { Account, AccountPolicy } from "./queries";

const ACCOUNT: Account = {
  name: "claude-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  auth: "token",
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

const NOW = Date.parse("2026-10-14T09:30:00Z");
const DAY = 24 * 60 * 60 * 1000;

describe("the renewal reminder", () => {
  it("is due when the token ends within 30 days and has not ended yet", () => {
    const at = (ms: number) => ({ expires_at: new Date(ms).toISOString() });
    expect(renewalDue({ expires_at: null }, NOW)).toBeNull();
    expect(renewalDue(at(NOW + 31 * DAY), NOW)).toBeNull();
    expect(renewalDue(at(NOW + 30 * DAY), NOW)).toEqual({ at: new Date(NOW + 30 * DAY).toISOString(), days: 30 });
    expect(renewalDue(at(NOW + 2 * 60 * 60 * 1000), NOW)?.days).toBe(1);
    expect(renewalDue(at(NOW - DAY), NOW)).toBeNull();
  });

  it("is never due for an OAuth account: the end is its access token's, which the hub renews", () => {
    expect(renewalDue({ auth: "oauth", expires_at: new Date(NOW + 2 * 60 * 60 * 1000).toISOString() }, NOW)).toBeNull();
  });
});
