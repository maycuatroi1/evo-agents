import { describe, expect, it } from "vitest";

import {
  addProblems,
  agentFormOf,
  changeBody,
  editProblems,
  emptyAgentForm,
  endOf,
  offeredWorkers,
  putBody,
  renewBody,
  renewProblems,
} from "./agent-model";
import type { AgentAccount } from "./agent-queries";

const NOW = Date.parse("2026-10-14T09:30:00Z");

const ACCOUNT: AgentAccount = {
  id: 7,
  name: "openai-a",
  env_var: "OPENAI_API_KEY",
  provider: "openai",
  category: "model",
  workers: ["gpu-1"],
  weight: 1,
  priority: 0,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: "2026-10-01T00:00:00Z",
  state: "expired",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  last_usage: null,
  last_usage_at: null,
  runs_24h: 0,
  runs_7d: 0,
  cost_24h_usd: 0,
  cost_7d_usd: 0,
  held_by: [],
};

describe("agent account forms", () => {
  it("reads a day as 00:00 UTC, and refuses a text that is no day", () => {
    expect(endOf("")).toBeNull();
    expect(endOf("2026-11-03")).toBe("2026-11-03T00:00:00.000Z");
    expect(endOf("2026-02-30")).toBeUndefined();
    expect(endOf("tomorrow")).toBeUndefined();
  });

  it("checks every field of a new account", () => {
    const empty = addProblems(emptyAgentForm(), " ", [], NOW);
    expect(empty).toEqual({ name: "nameRequired", envVar: "envVarRequired", value: "valueRequired", workers: "workersRequired" });
    const form = { ...emptyAgentForm(), name: "openai-a", envVar: "EVO_TOKEN", workers: ["gpu-1"], expires: "2026-10-01" };
    expect(addProblems(form, "sk-1", ["openai-a"], NOW)).toEqual({ name: "nameTaken", envVar: "envVarDenied", expires: "expiresPast" });
    expect(addProblems({ ...form, name: "Bad Name", envVar: "1X" }, "sk-1", [], NOW)).toMatchObject({ name: "nameInvalid", envVar: "envVarInvalid" });
    expect(addProblems({ ...form, envVar: "OPENAI_API_KEY", expires: "" }, "x".repeat(16385), [], NOW)).toEqual({ value: "valueTooLong" });
  });

  it("sends the value trimmed, the variable and the end", () => {
    const form = { ...emptyAgentForm(), name: "a", envVar: " OPENAI_API_KEY ", workers: ["gpu-1"], expires: "2026-11-03" };
    expect(putBody(form, " sk-1 ")).toEqual({ env_var: "OPENAI_API_KEY", value: "sk-1", workers: ["gpu-1"], expires_at: "2026-11-03T00:00:00.000Z" });
    expect(putBody({ ...form, expires: "" }, "sk-1").expires_at).toBeNull();
  });

  it("keeps a past end the account already has, and sends only what changed", () => {
    const form = agentFormOf(ACCOUNT);
    expect(form.expires).toBe("2026-10-01");
    expect(editProblems(form, ACCOUNT, NOW)).toEqual({});
    expect(changeBody(form, ACCOUNT)).toEqual({});
    expect(editProblems({ ...form, workers: [], weight: "x", priority: "1001" }, ACCOUNT, NOW)).toEqual({
      workers: "workersRequired",
      weight: "whole",
      priority: "range",
    });
    expect(changeBody({ ...form, weight: "3", workers: ["gpu-2", "gpu-1"], expires: "" }, ACCOUNT)).toEqual({
      weight: 3,
      workers: ["gpu-1", "gpu-2"],
      expires_at: null,
    });
  });

  it("renews with a value, and an end only when one is given", () => {
    expect(renewProblems("", "", NOW)).toEqual({ value: "valueRequired" });
    expect(renewProblems("sk-2", "2026-10-14", NOW)).toEqual({ expires: "expiresPast" });
    expect(renewBody(" sk-2 ", "")).toEqual({ value: "sk-2" });
    expect(renewBody("sk-2", "2026-12-01")).toEqual({ value: "sk-2", expires_at: "2026-12-01T00:00:00.000Z" });
  });

  it("offers the admin's own and shared workers serving the project, and those bound already", () => {
    const workers = [
      { name: "mine", owner: "ann", status: "online", shared: false, projects: ["alpha"] },
      { name: "shared", owner: "bob", status: "offline", shared: true, projects: ["alpha"] },
      { name: "bobs", owner: "bob", status: "online", shared: false, projects: ["alpha"] },
      { name: "elsewhere", owner: "ann", status: "online", shared: false, projects: ["beta"] },
      { name: "gone", owner: "ann", status: "revoked", shared: false, projects: ["alpha"] },
    ];
    expect(offeredWorkers(workers, "ann", "alpha", ["old"])).toEqual(["mine", "old", "shared"]);
  });
});
