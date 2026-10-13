import { describe, expect, it } from "vitest";

import { type Account, accountField, BY_RULES, KEEP, projectAccounts, rerunBody } from "./account-model";

const account = (name: string, projects: string[], extra: Partial<Account> = {}): Account =>
  ({ name, env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects, workers: [], weight: 100, priority: 100, paused_at: null, ...extra }) as Account;

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
});
