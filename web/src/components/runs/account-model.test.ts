import { describe, expect, it } from "vitest";

import { type Account, accountField, BY_RULES, KEEP, pickAccount, projectAccounts, rerunBody } from "./account-model";

const account = (name: string, projects: string[], extra: Partial<Account> = {}): Account =>
  ({ name, env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects, workers: [], weight: 100, priority: 100, paused_at: null, state: "ready", last_leased_at: null, ...extra }) as Account;

describe("account choice", () => {
  it("lists the accounts bound to the project, by name", () => {
    const accounts = [account("claude-b", ["demo"]), account("other", ["elsewhere"]), account("claude-a", ["elsewhere", "demo"])];
    expect(projectAccounts(accounts, "demo").map((one) => one.name)).toEqual(["claude-a", "claude-b"]);
    expect(projectAccounts(accounts, "none")).toEqual([]);
  });

  it("sends no account for the rules, so a dispatch body reads as before", () => {
    expect(accountField(BY_RULES)).toEqual({});
    expect(accountField(KEEP)).toEqual({});
    expect(accountField("claude-a")).toEqual({ account: "claude-a" });
  });

  it("sends no rerun body to keep the run's account, and asks the rules or names one otherwise", () => {
    expect(rerunBody(KEEP)).toBeUndefined();
    expect(rerunBody(BY_RULES)).toEqual({ account_by_rule: true });
    expect(rerunBody("claude-b")).toEqual({ account: "claude-b", account_by_rule: false });
  });

  it("fills a ready account for the runtime's sign-in that the worker may take, the lowest priority, then the least recently taken", () => {
    const accounts = [
      account("recent", ["demo"], { last_leased_at: "2026-10-20T10:00:00Z" }),
      account("older", ["demo"], { last_leased_at: "2026-10-19T10:00:00Z" }),
      account("cooling", ["demo"], { state: "cooling", last_leased_at: null }),
      account("paused", ["demo"], { state: "paused" }),
      account("elsewhere", ["other"]),
      account("glm", ["demo"], { env_var: "ZHIPU_API_KEY" }),
      account("openai", ["demo"], { env_var: "OPENAI_API_KEY", last_leased_at: "2026-10-20T10:00:00Z" }),
      account("pinned", ["demo"], { workers: ["gpu-01"], priority: 0 }),
    ];
    expect(pickAccount(accounts, "demo", "any", null)).toBe("older");
    expect(pickAccount(accounts, "demo", "claude-code", "lab-ws-01")).toBe("older");
    expect(pickAccount(accounts, "demo", "claude-code", "gpu-01")).toBe("pinned");
    expect(pickAccount([account("never", ["demo"]), ...accounts], "demo", "any", null)).toBe("never");
    expect(pickAccount(accounts, "demo", "codex", null)).toBe("openai");
    expect(pickAccount(accounts, "demo", "opencode", null)).toBe(BY_RULES);
    expect(pickAccount(accounts, "other", "codex", null)).toBe(BY_RULES);
    expect(pickAccount([], "demo", "any", null)).toBe(BY_RULES);
  });
});
