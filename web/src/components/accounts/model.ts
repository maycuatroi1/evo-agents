import {
  type Account,
  CLAUDE_TOKEN_VAR,
  type AccountPolicy,
  type AccountPolicyWrite,
  type AccountUsage,
  DEFAULT_POLICY,
  MAX_COOLDOWN_SECONDS,
  MAX_FAILOVERS,
  MAX_PRIORITY,
  MAX_WEIGHT,
  MIN_COOLDOWN_SECONDS,
  type Strategy,
} from "./queries";

/**
 * The Accounts page's reading of the hub's answers: how much of its usage limit an account used last, the projects a
 * policy can be set for, the forms of an account and of a policy with their checks, and which Claude accounts are due
 * for a new token.
 */

/** The highest share of a usage limit the CLI last reported for the account, in whole percent; null when it gave none. */
export function usagePercent(usage: AccountUsage | null | undefined): number | null {
  if (!usage) return null;
  const shares = [usage.utilization, ...Object.values(usage.windows ?? {}).map((window) => window.utilization)].filter(
    (share): share is number => typeof share === "number" && Number.isFinite(share),
  );
  if (shares.length === 0) return null;
  return Math.round(Math.min(Math.max(...shares), 1) * 100);
}

/** When the reported limit resets: the usage's own time, else the latest of its windows'. */
export function usageResetsAt(usage: AccountUsage | null | undefined): string | null {
  if (!usage) return null;
  if (usage.resets_at) return usage.resets_at;
  const times = Object.values(usage.windows ?? {})
    .map((window) => window.resets_at)
    .filter((at): at is string => typeof at === "string")
    .sort();
  return times.at(-1) ?? null;
}

export type PolicyRow = AccountPolicyWrite & { project: string; custom: boolean; updated_at: string | null };

/**
 * One row per project the visitor's accounts are bound to or they set a policy for, by name: their policy, or the
 * defaults (`custom` false) for a project without one.
 */
export function policyRows(accounts: Account[], policies: AccountPolicy[]): PolicyRow[] {
  const set = new Map(policies.map((policy) => [policy.project, policy]));
  const projects = new Set([...accounts.flatMap((account) => account.projects), ...set.keys()]);
  return [...projects].sort().map((project) => {
    const policy = set.get(project);
    return policy
      ? { ...policy, custom: true }
      : { ...DEFAULT_POLICY, project, custom: false, updated_at: null };
  });
}

export type AccountForm = { weight: string; priority: string };
export type PolicyForm = { strategy: Strategy; cooldownMinutes: string; failover: boolean; maxFailovers: string };
export type Problem = "range" | "whole";

function whole(text: string, min: number, max: number): { value: number | null; problem: Problem | null } {
  if (!/^\d+$/.test(text.trim())) return { value: null, problem: "whole" };
  const value = Number(text.trim());
  return value < min || value > max ? { value: null, problem: "range" } : { value, problem: null };
}

export const accountFormOf = (account: Account): AccountForm => ({ weight: String(account.weight), priority: String(account.priority) });

/** The problems of an account's form, by field; none when it may be sent. */
export function accountProblems(form: AccountForm): Partial<Record<keyof AccountForm, Problem>> {
  const problems: Partial<Record<keyof AccountForm, Problem>> = {};
  const weight = whole(form.weight, 0, MAX_WEIGHT).problem;
  const priority = whole(form.priority, 0, MAX_PRIORITY).problem;
  if (weight) problems.weight = weight;
  if (priority) problems.priority = priority;
  return problems;
}

export const accountBody = (form: AccountForm) => ({ weight: Number(form.weight.trim()), priority: Number(form.priority.trim()) });

export const policyFormOf = (policy: AccountPolicyWrite): PolicyForm => ({
  strategy: policy.strategy,
  cooldownMinutes: String(Math.round(policy.cooldown_seconds / 60)),
  failover: policy.failover,
  maxFailovers: String(policy.max_failovers),
});

export const COOLDOWN_MINUTES = { min: MIN_COOLDOWN_SECONDS / 60, max: MAX_COOLDOWN_SECONDS / 60 };

/** The problems of a policy's form, by field; none when it may be sent. */
export function policyProblems(form: PolicyForm): Partial<Record<"cooldownMinutes" | "maxFailovers", Problem>> {
  const problems: Partial<Record<"cooldownMinutes" | "maxFailovers", Problem>> = {};
  const cooldown = whole(form.cooldownMinutes, COOLDOWN_MINUTES.min, COOLDOWN_MINUTES.max).problem;
  // With failover off the field is hidden, and what it held is sent only when it is a valid number.
  const failovers = form.failover ? whole(form.maxFailovers, 0, MAX_FAILOVERS).problem : null;
  if (cooldown) problems.cooldownMinutes = cooldown;
  if (failovers) problems.maxFailovers = failovers;
  return problems;
}

export function policyBody(form: PolicyForm): AccountPolicyWrite {
  return {
    strategy: form.strategy,
    cooldown_seconds: Number(form.cooldownMinutes.trim()) * 60,
    failover: form.failover,
    max_failovers: whole(form.maxFailovers, 0, MAX_FAILOVERS).value ?? DEFAULT_POLICY.max_failovers,
  };
}

const DAY_MS = 24 * 60 * 60 * 1000;
/** An account whose token ends within this many days is reminded on the page. */
export const RENEW_WITHIN_DAYS = 30;

/**
 * When the account's token ends, if that is within RENEW_WITHIN_DAYS from `now` and still ahead; null for an account
 * without an end, one ending later, or one that has ended (its state says Expired then).
 */
export function renewalDue(account: Pick<Account, "expires_at">, now: number): { at: string; days: number } | null {
  if (!account.expires_at) return null;
  const left = Date.parse(account.expires_at) - now;
  if (!Number.isFinite(left) || left <= 0 || left > RENEW_WITHIN_DAYS * DAY_MS) return null;
  return { at: account.expires_at, days: Math.ceil(left / DAY_MS) };
}

/** Whether the account is a Claude account, which the Renew token dialog can give a new token. */
export const isClaudeAccount = (account: Pick<Account, "env_var">) => account.env_var === CLAUDE_TOKEN_VAR;
