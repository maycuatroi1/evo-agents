import { utf8Bytes } from "@/components/runs/run-model";

import {
  DENIED_ENV,
  DENIED_ENV_PREFIXES,
  ENV_NAME,
  MAX_ENV_VAR_CHARS,
  MAX_SECRET_BYTES,
  MAX_URL_PREFIX_CHARS,
  MAX_USERNAME_CHARS,
  SECRET_NAME,
  type Secret,
  type SecretKind,
  type SecretWrite,
} from "./queries";

/**
 * The Secrets page's form, its checks and the body it sends, without React. The checks repeat the API's own
 * (`evo_agents/hub/server/secrets.py`) so that a mistake is caught before the value leaves the page; the hub decides
 * again on every write.
 */
export type SecretForm = {
  name: string;
  kind: SecretKind;
  envVar: string;
  urlPrefix: string;
  username: string;
  projects: string[];
  workers: string[];
  /** The day the secret ends, as an <input type="date"> holds it (YYYY-MM-DD), or "" for none. */
  expires: string;
  /** Write-only: never filled from the hub, and cleared as soon as it was sent. */
  value: string;
};

export type SecretField = "name" | "envVar" | "urlPrefix" | "username" | "projects" | "expires" | "value";
export const SECRET_FIELDS: readonly SecretField[] = ["name", "envVar", "urlPrefix", "username", "projects", "expires", "value"];

export type SecretProblem =
  | "nameRequired"
  | "nameInvalid"
  | "nameTaken"
  | "envVarRequired"
  | "envVarInvalid"
  | "envVarDenied"
  | "urlPrefixRequired"
  | "urlPrefixInvalid"
  | "urlPrefixCredentials"
  | "usernameInvalid"
  | "projectsRequired"
  | "expiresInvalid"
  | "expiresPast"
  | "valueRequired"
  | "valueTooLong"
  | "valueNul"
  | "valueLines";

export type SecretProblems = Partial<Record<SecretField, SecretProblem>>;

export function emptySecretForm(): SecretForm {
  return { name: "", kind: "env", envVar: "", urlPrefix: "", username: "", projects: [], workers: [], expires: "", value: "" };
}

/** The day of `iso` in UTC, as an <input type="date"> holds it. */
export function utcDay(iso: string): string {
  return new Date(iso).toISOString().slice(0, 10);
}

/** The form to replace `secret` with: everything the hub shows of it, and no value. */
export function formOf(secret: Secret): SecretForm {
  return {
    name: secret.name,
    kind: secret.kind,
    envVar: secret.env_var ?? "",
    urlPrefix: secret.url_prefix ?? "",
    username: secret.username ?? "",
    projects: [...secret.projects],
    workers: [...secret.workers],
    expires: secret.expires_at ? utcDay(secret.expires_at) : "",
    value: "",
  };
}

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * When the secret ends: 00:00 UTC of the day chosen, as `evo-agents hub secret set --expires` and GitLab read an expiry
 * date. A day left as the secret had it keeps the secret's own time. Null for no end; undefined for a day that is not
 * one.
 */
export function expiresAt(day: string, original?: Secret | null): string | null | undefined {
  if (!day) return null;
  if (original?.expires_at && utcDay(original.expires_at) === day) return original.expires_at;
  const match = DAY.exec(day);
  if (!match) return undefined;
  const at = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(at.getTime()) || at.toISOString().slice(0, 10) !== day) return undefined;
  return at.toISOString();
}

/** Why `name` cannot be a secret's env_var, or null when it can (`credentials.env_name_refusal`). */
export function envVarProblem(name: string): "envVarRequired" | "envVarInvalid" | "envVarDenied" | null {
  if (!name) return "envVarRequired";
  if (name.length > MAX_ENV_VAR_CHARS || !ENV_NAME.test(name)) return "envVarInvalid";
  if ((DENIED_ENV as readonly string[]).includes(name) || DENIED_ENV_PREFIXES.some((prefix) => name.startsWith(prefix))) {
    return "envVarDenied";
  }
  return null;
}

/** Why `text` cannot be a git secret's url_prefix, or null when the hub may take it (it normalises it itself). */
export function urlPrefixProblem(text: string): "urlPrefixRequired" | "urlPrefixInvalid" | "urlPrefixCredentials" | null {
  const trimmed = text.trim();
  if (!trimmed) return "urlPrefixRequired";
  if (trimmed.length > MAX_URL_PREFIX_CHARS) return "urlPrefixInvalid";
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    return "urlPrefixInvalid";
  }
  if (url.protocol !== "https:" || !url.hostname) return "urlPrefixInvalid";
  if (url.username || url.password) return "urlPrefixCredentials";
  if (url.search || url.hash || trimmed.includes("?") || trimmed.includes("#")) return "urlPrefixInvalid";
  if (!/^[a-z0-9._-]+$/.test(url.hostname)) return "urlPrefixInvalid";
  return null;
}

const CONTROL = /[\u0000-\u001f\u007f]/;

/**
 * What is wrong with the form, field by field; empty when it may be sent. `taken` lists the names of the visitor's
 * secrets, so that adding one does not replace another by accident; `now` is the time the expiry must lie after.
 */
export function secretProblems(
  form: SecretForm,
  { adding, taken, original, now }: { adding: boolean; taken: readonly string[]; original?: Secret | null; now: number },
): SecretProblems {
  const found: SecretProblems = {};
  if (adding) {
    if (!form.name) found.name = "nameRequired";
    else if (!SECRET_NAME.test(form.name)) found.name = "nameInvalid";
    else if (taken.includes(form.name)) found.name = "nameTaken";
  }
  if (form.kind === "env") {
    const problem = envVarProblem(form.envVar.trim());
    if (problem) found.envVar = problem;
  } else {
    const problem = urlPrefixProblem(form.urlPrefix);
    if (problem) found.urlPrefix = problem;
    const username = form.username.trim();
    if (username && (username.length > MAX_USERNAME_CHARS || CONTROL.test(username))) found.username = "usernameInvalid";
  }
  if (form.projects.length === 0) found.projects = "projectsRequired";
  const end = expiresAt(form.expires, original);
  if (end === undefined) found.expires = "expiresInvalid";
  else if (end !== null && Date.parse(end) <= now) found.expires = "expiresPast";
  if (!form.value) found.value = "valueRequired";
  else if (utf8Bytes(form.value) > MAX_SECRET_BYTES) found.value = "valueTooLong";
  else if (form.value.includes("\u0000")) found.value = "valueNul";
  else if (form.kind === "git" && /[\r\n]/.test(form.value)) found.value = "valueLines";
  return found;
}

/** The PUT body of a form the checks passed: only the fields of its kind, and the value. */
export function secretBody(form: SecretForm, original?: Secret | null): SecretWrite {
  const target =
    form.kind === "env"
      ? { env_var: form.envVar.trim() }
      : { url_prefix: form.urlPrefix.trim(), ...(form.username.trim() ? { username: form.username.trim() } : {}) };
  return {
    kind: form.kind,
    ...target,
    projects: [...form.projects],
    workers: [...form.workers],
    expires_at: expiresAt(form.expires, original) ?? null,
    value: form.value,
  };
}

/** Whether the secret's end has passed: no run gets it any more. */
export function isExpired(secret: Pick<Secret, "expires_at">, now: number): boolean {
  return secret.expires_at !== null && Date.parse(secret.expires_at) <= now;
}
