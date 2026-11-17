import { isClaudeAccount } from "./model";
import type { Account } from "./queries";

/**
 * Weekly budgets and Z.ai quotas of model accounts (docs/credentials.md, section "Accounts"). An account without a
 * usage window of its own (an API key) may have a weekly budget in dollars, which the hub turns into its `seven_day`
 * window: the cost of its runs this week over the budget. A Z.ai Coding Plan account reads its windows from Z.ai.
 */

/** `accounts.MAX_WEEKLY_BUDGET_USD` of the API. */
export const MAX_WEEKLY_BUDGET_USD = 1_000_000;

/** The catalog id of Z.ai, whose Coding Plan accounts the hub reads the quota of. */
export const ZAI_PROVIDER = "zai";

/** Whether the account is a Z.ai provider account, whose quota the hub can read again on demand. */
export const isZaiAccount = (account: Pick<Account, "gateway">) => account.gateway?.provider === ZAI_PROVIDER;

/**
 * Whether a weekly budget means something for the account: a model account whose windows its runtime does not give,
 * so neither a Claude sign-in nor a Z.ai Coding Plan, which report their own.
 */
export const budgetable = (account: Pick<Account, "category" | "env_var" | "gateway">) =>
  account.category === "model" && !isClaudeAccount(account) && !isZaiAccount(account);

/** Whether the account shows usage bars: a report holds some, or it is an account whose windows the hub reads. */
export const hasUsage = (account: Pick<Account, "last_usage" | "env_var" | "gateway" | "weekly_budget_usd">) =>
  Boolean(account.last_usage) || isClaudeAccount(account) || isZaiAccount(account) || typeof account.weekly_budget_usd === "number";

export type BudgetProblem = "number" | "range";

/** The budget field of an account: its dollars, or empty for none. */
export const budgetFormOf = (account: Pick<Account, "weekly_budget_usd">) =>
  typeof account.weekly_budget_usd === "number" ? String(account.weekly_budget_usd) : "";

/** What a budget field holds: dollars above 0 up to the maximum, with cents at most; empty drops the budget. */
export function readBudget(text: string): { value: number | null; problem: BudgetProblem | null } {
  const trimmed = text.trim().replace(/^\$/, "");
  if (trimmed === "") return { value: null, problem: null };
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return { value: null, problem: "number" };
  const value = Number(trimmed);
  return value <= 0 || value > MAX_WEEKLY_BUDGET_USD ? { value: null, problem: "range" } : { value, problem: null };
}
