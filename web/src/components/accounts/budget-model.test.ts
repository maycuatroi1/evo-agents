import { describe, expect, it } from "vitest";

import { budgetable, budgetFormOf, hasUsage, isZaiAccount, readBudget } from "./budget-model";
import { CLAUDE_TOKEN_VAR } from "./queries";

const GATEWAY = { provider: "zai", prefix: "zai", base_url_host: "api.z.ai", api_format: "anthropic", models: 2, status: "ok" as const, reason: null };

describe("weekly budgets and Z.ai quotas", () => {
  it("knows a Z.ai account by its provider", () => {
    expect(isZaiAccount({ gateway: GATEWAY })).toBe(true);
    expect(isZaiAccount({ gateway: { ...GATEWAY, provider: "openrouter" } })).toBe(false);
    expect(isZaiAccount({ gateway: null })).toBe(false);
  });

  it("offers a budget to a model account that reports no windows of its own", () => {
    const key = { category: "model" as const, env_var: "OPENAI_API_KEY", gateway: null };
    expect(budgetable(key)).toBe(true);
    expect(budgetable({ ...key, env_var: CLAUDE_TOKEN_VAR })).toBe(false);
    expect(budgetable({ ...key, gateway: GATEWAY })).toBe(false);
    expect(budgetable({ ...key, category: "service" })).toBe(false);
  });

  it("draws usage for a report, a Claude or Z.ai account, or a budget", () => {
    const plain = { last_usage: null, env_var: "OPENAI_API_KEY", gateway: null, weekly_budget_usd: null };
    expect(hasUsage(plain)).toBe(false);
    expect(hasUsage({ ...plain, weekly_budget_usd: 25 })).toBe(true);
    expect(hasUsage({ ...plain, gateway: GATEWAY })).toBe(true);
    expect(hasUsage({ ...plain, env_var: CLAUDE_TOKEN_VAR })).toBe(true);
  });

  it("reads a budget as dollars above 0 with cents at most, empty dropping it", () => {
    expect(budgetFormOf({ weekly_budget_usd: 40.5 })).toBe("40.5");
    expect(budgetFormOf({ weekly_budget_usd: null })).toBe("");
    expect(readBudget(" $25 ")).toEqual({ value: 25, problem: null });
    expect(readBudget("12.34")).toEqual({ value: 12.34, problem: null });
    expect(readBudget("")).toEqual({ value: null, problem: null });
    expect(readBudget("1.234")).toEqual({ value: null, problem: "number" });
    expect(readBudget("ten")).toEqual({ value: null, problem: "number" });
    expect(readBudget("0")).toEqual({ value: null, problem: "range" });
    expect(readBudget("1000000.01")).toEqual({ value: null, problem: "range" });
  });
});
