import { MAX_SECRET_BYTES, SECRET_NAME, type SecretSummary, type SecretWrite } from "./login-queries";
import { type Account, CLAUDE_TOKEN_VAR } from "./queries";

/**
 * The Add Claude account and Renew token dialog without React: its form and checks, the PUT body, and the
 * `evo-agents hub account login` command the fields make. When an account is due for a new token is the page's own
 * (`model.ts`), so this module loads with the dialog only. The checks repeat the hub's
 * (`evo_agents/hub/server/secrets.py`) so a mistake is caught before the token leaves the page; the hub decides again
 * on every write. The token itself is never part of the form: the dialog reads it from its field.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

export type LoginForm = {
  name: string;
  projects: string[];
  workers: string[];
  /** The day the token ends, as an <input type="date"> holds it (YYYY-MM-DD). */
  expires: string;
};

export type LoginField = "name" | "projects" | "expires" | "token";
export const LOGIN_FIELDS: readonly LoginField[] = ["name", "projects", "expires", "token"];

export type LoginProblem =
  | "nameRequired"
  | "nameInvalid"
  | "nameOther"
  | "projectsRequired"
  | "expiresRequired"
  | "expiresInvalid"
  | "expiresPast"
  | "tokenRequired"
  | "tokenApiKey"
  | "tokenSpaces"
  | "tokenTooLong";

export type LoginProblems = Partial<Record<LoginField, LoginProblem>>;

/** The day of `ms` in UTC, as an <input type="date"> holds it. */
export function utcDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/**
 * The day a new token ends by default: a year from today less a day, in UTC, as `hub account login` sets it. A token
 * of `claude setup-token` lasts a year, so the hub stops leasing it a day before it would fail.
 */
export function defaultExpiryDay(now: number): string {
  const today = new Date(now);
  return utcDay(Date.UTC(today.getUTCFullYear() + 1, today.getUTCMonth(), today.getUTCDate()) - DAY_MS);
}

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** 00:00 UTC of `day`, as `hub secret set --expires` reads a day; undefined for a text that is no day. */
export function expiresAtOf(day: string): string | undefined {
  if (!DAY.test(day)) return undefined;
  const at = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(at.getTime()) || at.toISOString().slice(0, 10) !== day) return undefined;
  return at.toISOString();
}

export function emptyLoginForm(now: number): LoginForm {
  return { name: "", projects: [], workers: [], expires: defaultExpiryDay(now) };
}

/** A renewal's form: the account's name and bindings, which it keeps, and a new year for its new token. */
export function renewForm(account: Pick<Account, "name" | "projects" | "workers">, now: number): LoginForm {
  return { name: account.name, projects: [...account.projects], workers: [...account.workers], expires: defaultExpiryDay(now) };
}

/** What a name typed in the Add dialog is already: nothing, one of the visitor's Claude accounts, or another secret. */
export type NameMatch = { kind: "free" } | { kind: "account"; account: Account } | { kind: "other"; secret: SecretSummary };

export function nameMatch(name: string, accounts: readonly Account[], secrets: readonly SecretSummary[]): NameMatch {
  const account = accounts.find((item) => item.name === name);
  if (account?.env_var === CLAUDE_TOKEN_VAR) return { kind: "account", account };
  if (account) return { kind: "other", secret: { name, kind: "env", env_var: account.env_var } };
  const secret = secrets.find((item) => item.name === name);
  if (secret?.kind === "env" && secret.env_var === CLAUDE_TOKEN_VAR) return { kind: "free" }; // listed once the accounts reload
  return secret ? { kind: "other", secret } : { kind: "free" };
}

function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** Why `token` cannot be sent, or null when it can. An API key of the Console belongs in ANTHROPIC_API_KEY instead. */
export function tokenProblem(token: string): LoginProblem | null {
  const value = token.trim();
  if (!value) return "tokenRequired";
  if (value.startsWith("sk-ant-api")) return "tokenApiKey";
  if (/[\s\u0000-\u001f\u007f]/.test(value)) return "tokenSpaces";
  if (utf8Bytes(value) > MAX_SECRET_BYTES) return "tokenTooLong";
  return null;
}

/** What is wrong with the form and the token, field by field; empty when they may be sent. */
export function loginProblems(
  form: LoginForm,
  token: string,
  { renewing, match, now }: { renewing: boolean; match: NameMatch; now: number },
): LoginProblems {
  const found: LoginProblems = {};
  if (!renewing) {
    if (!form.name) found.name = "nameRequired";
    else if (!SECRET_NAME.test(form.name)) found.name = "nameInvalid";
    else if (match.kind === "other") found.name = "nameOther";
  }
  if (form.projects.length === 0) found.projects = "projectsRequired";
  const end = form.expires ? expiresAtOf(form.expires) : null;
  if (end === null) found.expires = "expiresRequired";
  else if (end === undefined) found.expires = "expiresInvalid";
  else if (Date.parse(end) <= now) found.expires = "expiresPast";
  const problem = tokenProblem(token);
  if (problem) found.token = problem;
  return found;
}

/** The PUT body of a form the checks passed: an env secret for CLAUDE_CODE_OAUTH_TOKEN, the token trimmed. */
export function loginBody(form: LoginForm, token: string): SecretWrite {
  return {
    kind: "env",
    env_var: CLAUDE_TOKEN_VAR,
    projects: [...form.projects],
    workers: [...form.workers],
    expires_at: expiresAtOf(form.expires) ?? null,
    value: token.trim(),
  };
}

/** `text` as one word of a POSIX shell: as it is when it is safe so, else in single quotes. */
export function shellWord(text: string): string {
  return /^[A-Za-z0-9._/:=@%+-]+$/.test(text) ? text : `'${text.replace(/'/g, `'\\''`)}'`;
}

/**
 * The `evo-agents hub account login` command that does what the dialog does, with `claude setup-token` run for the
 * member and no token to paste. A renewal names the account alone, as the CLI then keeps its projects and workers; an
 * end date other than the default is passed. `complete` is false while a placeholder stands for a missing field.
 */
export function loginCommand(form: LoginForm, { renewing, now }: { renewing: boolean; now: number }): { text: string; complete: boolean } {
  const named = SECRET_NAME.test(form.name);
  const words = ["evo-agents", "hub", "account", "login", named ? form.name : "<name>"];
  let complete = named;
  if (!renewing) {
    if (form.projects.length === 0) {
      words.push("--project", "<project>");
      complete = false;
    }
    for (const project of form.projects) words.push("--project", shellWord(project));
    for (const worker of form.workers) words.push("--worker", shellWord(worker));
  }
  if (form.expires && form.expires !== defaultExpiryDay(now) && expiresAtOf(form.expires)) words.push("--expires", form.expires);
  return { text: words.join(" "), complete };
}
