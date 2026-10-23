import type { components } from "@/lib/api/schema";

/**
 * Shares of a secret or an account as the Accounts and Secrets pages read and write them (docs/credentials.md, section
 * "Sharing"; the rules are `evo_agents.hub.credential_shares`): the form of a new share with its checks, the body
 * POST /v1/secrets/{name}/shares takes, and what about a live share is unusual enough to say.
 */
type Schemas = components["schemas"];
export type CredentialShare = Schemas["CredentialShare"];
export type ShareWrite = Schemas["ShareWrite"];
export type GranteeKind = CredentialShare["grantee_kind"];
export type Level = NonNullable<CredentialShare["level"]>;

/**
 * `credential_shares.TERMS_WARNING` of the hub, word for word: shown before a personal subscription token is shared,
 * in both languages, since the hub keeps this English text in the audit row share.terms_ack. model.test.ts reads the
 * Python constant and fails when the two differ.
 */
export const TERMS_WARNING =
  "This is a personal Claude subscription token. Anthropic's Consumer Terms (section 2) do not allow you to let " +
  "anyone else use your account. Anthropic may limit, suspend or close the account without notice. Every request " +
  "a run makes with this token is made in your name, as the account holder.";

export const GRANTEE_KINDS = ["user", "unit", "all"] as const satisfies readonly GranteeKind[];
export const LEVELS = ["A0", "A1", "A2", "A3", "A4", "A5"] as const satisfies readonly Level[];
/** `credential_shares.MAX_BUDGET_USD` and `MAX_RUNNING`. */
export const MAX_BUDGET_USD = 1_000_000;
export const MAX_RUNNING = 100;
/** `admin.LOGIN_NAME` and `org.SLUG_PATTERN` of the hub. */
export const LOGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
export const UNIT_SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
/** A share that ends within this many days is said to end soon. */
export const ENDS_SOON_DAYS = 7;
/** The variable of a personal Claude subscription token (`account_providers`: provider claude). */
export const SUBSCRIPTION_VAR = "CLAUDE_CODE_OAUTH_TOKEN";

const DAY_MS = 86_400_000;

/** A secret or an account that may be shared: its name, the projects it is bound to, and whether it is a subscription. */
export type ShareTarget = { name: string; projects: string[]; subscription: boolean };

/** Whether a secret of `kind` setting `envVar` is a personal subscription token (`credential_class` subscription). */
export const isSubscription = (kind: string, envVar: string | null | undefined) => kind === "env" && envVar === SUBSCRIPTION_VAR;

export type ShareForm = {
  secret: string;
  granteeKind: GranteeKind;
  login: string;
  unit: string;
  projects: string[];
  level: Level | "";
  budget: string;
  ceiling: string;
  maxRunning: string;
  /** YYYY-MM-DD, the day it ends at 00:00 UTC; empty for no end. */
  expires: string;
  termsAck: boolean;
};

export type ShareField = "login" | "unit" | "projects" | "budget" | "ceiling" | "maxRunning" | "expires" | "termsAck";
export type ShareProblem = "required" | "pattern" | "range" | "whole" | "past" | "ack";

/** A new form for `target`: one member, every project it is bound to, no limit. */
export function emptyShareForm(target: ShareTarget | null): ShareForm {
  return {
    secret: target?.name ?? "",
    granteeKind: "user",
    login: "",
    unit: "",
    projects: target ? [...target.projects].sort() : [],
    level: "",
    budget: "",
    ceiling: "",
    maxRunning: "",
    expires: "",
    termsAck: false,
  };
}

/** The UTC day of `now`, as YYYY-MM-DD. */
export const utcDay = (now: number) => new Date(now).toISOString().slice(0, 10);

function wholeIn(value: string, min: number, max: number): ShareProblem | null {
  const text = value.trim();
  if (text === "") return null;
  if (!/^\d+$/.test(text)) return "whole";
  const number = Number(text);
  return number < min || number > max ? "range" : null;
}

/** What is wrong with each field of `form` for `target`, as of `now`; empty when it may be sent. */
export function shareProblems(form: ShareForm, target: ShareTarget | null, now: number): Partial<Record<ShareField, ShareProblem>> {
  const problems: Partial<Record<ShareField, ShareProblem>> = {};
  if (form.granteeKind === "user") {
    const login = form.login.trim();
    if (!login) problems.login = "required";
    else if (!LOGIN_NAME.test(login)) problems.login = "pattern";
  }
  if (form.granteeKind === "unit") {
    const unit = form.unit.trim();
    if (!unit) problems.unit = "required";
    else if (!UNIT_SLUG.test(unit)) problems.unit = "pattern";
  }
  if (form.projects.length === 0) problems.projects = "required";
  const budget = form.budget.trim();
  if (budget !== "") {
    const number = Number(budget);
    if (!/^\d+(\.\d{1,2})?$/.test(budget)) problems.budget = "pattern";
    else if (number <= 0 || number > MAX_BUDGET_USD) problems.budget = "range";
  }
  const ceiling = wholeIn(form.ceiling, 1, 100);
  if (ceiling) problems.ceiling = ceiling;
  const running = wholeIn(form.maxRunning, 1, MAX_RUNNING);
  if (running) problems.maxRunning = running;
  if (form.expires && form.expires <= utcDay(now)) problems.expires = "past";
  if (target?.subscription && !form.termsAck) problems.termsAck = "ack";
  return problems;
}

/** The body of POST /v1/secrets/{name}/shares for a form without problems; a field left empty sends no limit. */
export function shareBody(form: ShareForm, target: ShareTarget | null): ShareWrite {
  const number = (value: string) => (value.trim() === "" ? undefined : Number(value.trim()));
  return {
    grantee_kind: form.granteeKind,
    ...(form.granteeKind === "user" ? { login: form.login.trim() } : {}),
    ...(form.granteeKind === "unit" ? { unit: form.unit.trim() } : {}),
    projects: form.projects,
    ...(form.level ? { level: form.level } : {}),
    ...(number(form.budget) === undefined ? {} : { budget_usd: number(form.budget) }),
    ...(number(form.ceiling) === undefined ? {} : { usage_ceiling_pct: number(form.ceiling) }),
    ...(number(form.maxRunning) === undefined ? {} : { max_running: number(form.maxRunning) }),
    ...(form.expires ? { expires_at: `${form.expires}T00:00:00Z` } : {}),
    terms_ack: Boolean(target?.subscription && form.termsAck),
  };
}

/** Whether a live share ends within ENDS_SOON_DAYS of `now`. */
export function endsSoon(share: Pick<CredentialShare, "expires_at">, now: number): boolean {
  if (!share.expires_at) return false;
  const left = new Date(share.expires_at).getTime() - now;
  return left > 0 && left <= ENDS_SOON_DAYS * DAY_MS;
}

/** The live shares of some secrets or accounts, newest first. */
export function liveShares(items: { shares: CredentialShare[] }[]): CredentialShare[] {
  return items.flatMap((item) => item.shares).filter((share) => share.revoked_at === null).sort((a, b) => b.id - a.id);
}
