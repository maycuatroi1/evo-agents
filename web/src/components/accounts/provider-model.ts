import { expiresAtOf } from "./login-model";
import { MAX_SECRET_BYTES, SECRET_NAME, type SecretSummary } from "./login-queries";
import {
  type ApiFormat,
  type CatalogProvider,
  MAX_MODELS,
  MODEL_NAME,
  type ProbeWrite,
  type ProviderAccount,
  type ProviderChange,
  type ProviderWrite,
} from "./provider-queries";

/**
 * The catalog and the provider form without React: the groups the catalog filters by, its search, the form of a
 * provider account with its checks, and the bodies of PUT and PATCH. The checks repeat the hub's
 * (`evo_agents/hub/server/provider_accounts.py`) so a mistake is caught before anything is sent; the hub decides again
 * on every write, and alone checks that a base URL resolves to public addresses. The key is never part of the form:
 * the form reads it from its field when it sends, then clears the field.
 */

/** The catalog's filters, in order: every entry, then the catalog's groups as the page names them. */
export const CATALOG_FILTERS = ["all", "popular", "gateways", "inference", "regional", "local", "custom"] as const;
export type CatalogFilter = (typeof CATALOG_FILTERS)[number];

/** The filter of each group of the catalog (`model_catalog.json`); a group the page does not know is a gateway's. */
const FILTER_OF: Record<string, Exclude<CatalogFilter, "all">> = {
  login: "popular",
  apikey: "popular",
  free: "popular",
  aggregator: "gateways",
  inference: "inference",
  regional: "regional",
  local: "local",
  custom: "custom",
};

export const filterOf = (entry: Pick<CatalogProvider, "group">) => FILTER_OF[entry.group] ?? "gateways";

/** The entries of `filter` whose name, id or alias holds every word of `query`, in the catalog's order. */
export function catalogMatches(entries: readonly CatalogProvider[], query: string, filter: CatalogFilter): CatalogProvider[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return entries.filter((entry) => {
    if (filter !== "all" && filterOf(entry) !== filter) return false;
    const text = `${entry.name} ${entry.id} ${entry.alias}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

/** How many entries each filter holds, for its chip. */
export function filterCounts(entries: readonly CatalogProvider[]): Record<CatalogFilter, number> {
  const counts = Object.fromEntries(CATALOG_FILTERS.map((filter) => [filter, 0])) as Record<CatalogFilter, number>;
  for (const entry of entries) {
    counts.all += 1;
    counts[filterOf(entry)] += 1;
  }
  return counts;
}

/** The host of a URL, or null for a text that is none. */
export function hostOf(url: string): string | null {
  try {
    return new URL(url).host || null;
  } catch {
    return null;
  }
}

/** The first name free among `taken` made of `base`: lower case, the characters a secret's name takes, then -2, -3. */
export function freeName(base: string, taken: ReadonlySet<string>): string {
  const clean = base.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "").slice(0, 56) || "provider";
  if (!taken.has(clean)) return clean;
  for (let index = 2; ; index += 1) if (!taken.has(`${clean}-${index}`)) return `${clean}-${index}`;
}

export type KeySource = "paste" | "secret";

export type ProviderForm = {
  name: string;
  baseUrl: string;
  format: ApiFormat;
  source: KeySource;
  /** The env secret whose value is the key, when `source` is secret. */
  secret: string;
  projects: string[];
  workers: string[];
  /** The day the key stops being leased, as an <input type="date"> holds it; empty for none. */
  expires: string;
  models: string[];
};

export type ProviderField = "name" | "baseUrl" | "key" | "secret" | "projects" | "expires" | "models";
export const PROVIDER_FIELDS: readonly ProviderField[] = ["name", "baseUrl", "key", "secret", "projects", "expires", "models"];

export type ProviderProblem =
  | "nameRequired"
  | "nameInvalid"
  | "nameTaken"
  | "urlRequired"
  | "urlInvalid"
  | "urlHttps"
  | "keyRequired"
  | "keyLine"
  | "keyTooLong"
  | "secretRequired"
  | "projectsRequired"
  | "expiresInvalid"
  | "expiresPast"
  | "modelsTooMany";

export type ProviderProblems = Partial<Record<ProviderField, ProviderProblem>>;

/** A new account's form for `entry`: its name free among the visitor's secrets, the catalog's URL, format and model. */
export function newProviderForm(entry: CatalogProvider, taken: ReadonlySet<string>): ProviderForm {
  return {
    name: freeName(entry.id, taken),
    baseUrl: entry.base_url,
    format: entry.api_format,
    source: "paste",
    secret: "",
    projects: [],
    workers: [],
    expires: "",
    models: entry.default_model ? [entry.default_model] : [],
  };
}

/** The form of a saved account: what it holds, and no key, which Edit keeps. */
export function editProviderForm(account: ProviderAccount): ProviderForm {
  return {
    name: account.name,
    baseUrl: account.base_url,
    format: account.api_format,
    source: "paste",
    secret: "",
    projects: [...account.projects],
    workers: [...account.workers],
    expires: account.expires_at ? account.expires_at.slice(0, 10) : "",
    models: [...account.models],
  };
}

const utf8Bytes = (text: string) => new TextEncoder().encode(text).length;

/** The problem of a key as pasted, or null: one line of at most MAX_SECRET_BYTES, as an HTTP header carries it. */
export function keyProblem(key: string): ProviderProblem | null {
  const value = key.trim();
  if (!value) return "keyRequired";
  if (/[\u0000\r\n]/.test(value)) return "keyLine";
  return utf8Bytes(value) > MAX_SECRET_BYTES ? "keyTooLong" : null;
}

export function urlProblem(url: string): ProviderProblem | null {
  const text = url.trim();
  if (!text) return "urlRequired";
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    return "urlInvalid";
  }
  if (!parsed.host) return "urlInvalid";
  return parsed.protocol === "https:" ? null : "urlHttps";
}

/** The problems of the key part of a form: a key pasted, or an env secret picked. */
export function keySourceProblems(form: Pick<ProviderForm, "source" | "secret">, key: string): ProviderProblems {
  if (form.source === "secret") return form.secret ? {} : { secret: "secretRequired" };
  const problem = keyProblem(key);
  return problem ? { key: problem } : {};
}

/**
 * The problems of the form, by field; none when it may be sent. `taken` holds the names a new account may not take
 * (`takenNames`); `editing` skips the name and the key, which Edit keeps.
 */
export function providerProblems(
  form: ProviderForm,
  { key, taken, editing, today }: { key: string; taken: ReadonlySet<string>; editing: boolean; today: string },
): ProviderProblems {
  const problems: ProviderProblems = {};
  if (!editing) {
    const name = form.name.trim();
    if (!name) problems.name = "nameRequired";
    else if (!SECRET_NAME.test(name)) problems.name = "nameInvalid";
    else if (taken.has(name)) problems.name = "nameTaken";
    Object.assign(problems, keySourceProblems(form, key));
  }
  const url = urlProblem(form.baseUrl);
  if (url) problems.baseUrl = url;
  if (form.projects.length === 0) problems.projects = "projectsRequired";
  if (form.expires) {
    const at = expiresAtOf(form.expires);
    if (!at) problems.expires = "expiresInvalid";
    else if (form.expires <= today) problems.expires = "expiresPast";
  }
  if (form.models.length > MAX_MODELS) problems.models = "modelsTooMany";
  return problems;
}

/**
 * The names a new account may not take: the visitor's secrets, which the hub refuses with 409, and their provider
 * accounts, which a PUT would replace whole.
 */
export function takenNames(secrets: readonly SecretSummary[], providers: readonly Pick<ProviderAccount, "name">[]): Set<string> {
  return new Set([...secrets.map((secret) => secret.name), ...providers.map((provider) => provider.name)]);
}

/** What the key part sends: the key pasted, or the env secret that holds it. */
export const keyBody = (form: Pick<ProviderForm, "source" | "secret">, key: string) =>
  form.source === "secret" ? { from_secret: form.secret } : { value: key.trim() };

/** PUT /v1/provider-accounts/{name}: the account whole. */
export function providerWriteBody(form: ProviderForm, provider: string, key: string): ProviderWrite {
  const expires = form.expires ? expiresAtOf(form.expires) : undefined;
  return {
    provider,
    base_url: form.baseUrl.trim(),
    api_format: form.format,
    ...keyBody(form, key),
    models: form.models,
    projects: form.projects,
    workers: form.workers,
    ...(expires ? { expires_at: expires } : {}),
  };
}

const sameList = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((item, index) => item === b[index]);
const sameSet = (a: readonly string[], b: readonly string[]) => sameList([...a].sort(), [...b].sort());

/** PATCH /v1/provider-accounts/{name}: only what the form changed, never a key; null when it changed nothing. */
export function providerChangeBody(form: ProviderForm, account: ProviderAccount): ProviderChange | null {
  const change: ProviderChange = {};
  const url = form.baseUrl.trim();
  if (url !== account.base_url) change.base_url = url;
  if (form.format !== account.api_format) change.api_format = form.format;
  if (!sameList(form.models, account.models)) change.models = form.models;
  if (!sameSet(form.projects, account.projects)) change.projects = form.projects;
  if (!sameSet(form.workers, account.workers)) change.workers = form.workers;
  const was = account.expires_at ? account.expires_at.slice(0, 10) : "";
  if (form.expires !== was) change.expires_at = form.expires ? (expiresAtOf(form.expires) ?? null) : null;
  return Object.keys(change).length ? change : null;
}

export type ModelAdd = { models: string[]; problem: "modelInvalid" | "modelsTooMany" | null };

/** The models with `text` added at the end, once; the problem of a name the hub would refuse. */
export function addModel(models: readonly string[], text: string): ModelAdd {
  const name = text.trim();
  if (!MODEL_NAME.test(name)) return { models: [...models], problem: "modelInvalid" };
  if (models.includes(name)) return { models: [...models], problem: null };
  if (models.length >= MAX_MODELS) return { models: [...models], problem: "modelsTooMany" };
  return { models: [...models, name], problem: null };
}

/** The models to offer as choices: those picked, then those the endpoint listed, then the catalog's, each once. */
export function modelChoices(picked: readonly string[], fetched: readonly string[], fallback: string): string[] {
  return [...new Set([...picked, ...fetched, ...(fallback ? [fallback] : [])])];
}

/** POST /v1/provider-accounts/probe: Test connection (with the first model picked) or Fetch models, before saving. */
export function probeBody(form: ProviderForm, provider: string, key: string, model: string | null): ProbeWrite {
  return { provider, base_url: form.baseUrl.trim(), api_format: form.format, ...keyBody(form, key), model };
}
