import {
  type Account,
  CLAUDE_TOKEN_VAR,
  type AccountPolicy,
  type AccountPolicyWrite,
  DEFAULT_POLICY,
  MAX_COOLDOWN_SECONDS,
  MAX_FAILOVERS,
  MAX_PRIORITY,
  MAX_WEIGHT,
  MIN_COOLDOWN_SECONDS,
  type Strategy,
} from "./queries";

/**
 * The Accounts page's reading of the hub's answers: the projects a policy can be set for, the forms of an account and
 * of a policy with their checks, and which Claude accounts are due for a new token. Usage limits: usage-model.ts.
 */

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
 * without an end, one ending later, or one that has ended (its state says Expired then). An OAuth account's end is
 * its access token's, which the hub renews when a run takes it: nothing to remind.
 */
export function renewalDue(account: Pick<Account, "expires_at"> & Partial<Pick<Account, "auth">>, now: number): { at: string; days: number } | null {
  if (!account.expires_at || account.auth === "oauth") return null;
  const left = Date.parse(account.expires_at) - now;
  if (!Number.isFinite(left) || left <= 0 || left > RENEW_WITHIN_DAYS * DAY_MS) return null;
  return { at: account.expires_at, days: Math.ceil(left / DAY_MS) };
}

/** Whether the account is a Claude account, which the Renew token dialog can give a new token. */
export const isClaudeAccount = (account: Pick<Account, "env_var">) => account.env_var === CLAUDE_TOKEN_VAR;

/**
 * The groups the Accounts page lists, in order, by the category the hub gives each account. A kind of account the
 * page learns later (an agent's own) is one more entry here and one more section in the page, not a new page.
 */
export const ACCOUNT_GROUPS = [
  { id: "model", category: "model" },
  { id: "service", category: "service" },
] as const satisfies readonly { id: string; category: Account["category"] }[];
export type AccountGroupId = (typeof ACCOUNT_GROUPS)[number]["id"];

/** The accounts of each group, in the order of ACCOUNT_GROUPS; a group without accounts has an empty list. */
export function groupAccounts(accounts: Account[]): { id: AccountGroupId; accounts: Account[] }[] {
  return ACCOUNT_GROUPS.map(({ id, category }) => ({ id, accounts: accounts.filter((account) => account.category === category) }));
}

/** The variables the visitor has two or more accounts for: only there do weight and priority mean anything. */
export function sharedVariables(accounts: Account[]): Set<string> {
  const seen = new Map<string, number>();
  for (const account of accounts) seen.set(account.env_var, (seen.get(account.env_var) ?? 0) + 1);
  return new Set([...seen].filter(([, count]) => count >= 2).map(([variable]) => variable));
}

/** One line of the service table: an account, or the accounts of one provider bound to the same projects. */
export type ServiceEntry = { key: string; provider: Account["provider"]; projects: string[]; accounts: Account[] };

/** Service accounts of the same provider with the same set of projects form one entry, as "Lark, 2 variables". */
export function serviceEntries(accounts: Account[]): ServiceEntry[] {
  const entries = new Map<string, ServiceEntry>();
  for (const account of accounts) {
    const projects = [...account.projects].sort();
    const key = `${account.provider}:${projects.join(",")}`;
    const entry = entries.get(key);
    if (entry) entry.accounts.push(account);
    else entries.set(key, { key, provider: account.provider, projects, accounts: [account] });
  }
  return [...entries.values()];
}

/** The state an entry shows: the first account that is not ready, else ready. */
export function entryAccount(entry: ServiceEntry): Account {
  return entry.accounts.find((account) => account.state !== "ready") ?? entry.accounts[0];
}

/** The latest time any account of the entry was leased, or null. */
export function lastLeased(accounts: Account[]): string | null {
  return accounts.reduce<string | null>((latest, { last_leased_at: at }) => (at && (!latest || Date.parse(at) > Date.parse(latest)) ? at : latest), null);
}
