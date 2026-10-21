/**
 * The autonomy policy as the web reads and writes it, without React: its keys with their defaults and checks (the
 * same as `evo_agents/hub/autonomy.py`, so a mistake is caught before the policy leaves the page; the hub checks
 * again on every write), the form an admin edits, and the body it sends.
 *
 * A body holds only the keys its writer set; a key it leaves out reads as its default. The form shows every key at
 * the value the policy gives it, and the body it sends keeps a key the policy set and the admin left as it was, adds a
 * key whose value the admin changed from its default, and drops a key the admin put back to its default.
 */

export type KeyKind = "choice" | "whole" | "number" | "line" | "optional" | "globs";

export type KeySpec = {
  /** Dotted within its section, or within a repo: `merge.hold_until`, `jobs.uat_check`. */
  path: string;
  kind: KeyKind;
  default: unknown;
  options?: readonly string[];
  min?: number;
  max?: number;
  /** For a number: above `min`, not from it. */
  aboveMin?: boolean;
  /** For a line of text, optional or not. */
  maxChars?: number;
};

export const HOLDS = ["merged", "uat_check", "prod_check"] as const;
export const RELEASES = ["immediate", "after_uat", "manual"] as const;
export const MODES = ["off", "auto"] as const;
export const JOBS = ["uat_check", "prod_deploy", "prod_check", "rollback"] as const;
export const MAX_RISK_PATHS = 100;
export const MAX_GLOB_CHARS = 500;
export const MAX_NOTE_CHARS = 1000;

export type SectionName = "merge" | "judge" | "breaker" | "audit";
export const SECTION_NAMES: readonly SectionName[] = ["merge", "judge", "breaker", "audit"];

/** `autonomy.KEYS`, grouped by section in the order the hub lists them. */
export const SECTIONS: Record<SectionName, readonly KeySpec[]> = {
  merge: [
    { path: "merge.opt_in_label", kind: "line", default: "evo-merge", maxChars: 255 },
    { path: "merge.risk_paths", kind: "globs", default: [] },
    { path: "merge.hold_until", kind: "choice", default: "merged", options: HOLDS },
    { path: "merge.pipeline_minutes", kind: "whole", default: 60, min: 1, max: 1440 },
  ],
  judge: [
    { path: "judge.worker", kind: "optional", default: null, maxChars: 100 },
    { path: "judge.model", kind: "optional", default: null, maxChars: 200 },
  ],
  breaker: [
    { path: "breaker.target_red_minutes", kind: "whole", default: 30, min: 1, max: 10080 },
    { path: "breaker.reverts_in_a_row", kind: "whole", default: 2, min: 1, max: 100 },
    { path: "breaker.revert_rate_factor", kind: "number", default: 2, min: 0, max: 100, aboveMin: true },
    { path: "breaker.revert_rate_max", kind: "number", default: 0.1, min: 0, max: 1 },
    { path: "breaker.baseline_min_merges", kind: "whole", default: 10, min: 1, max: 10000 },
    { path: "breaker.judge_misses_7d", kind: "whole", default: 2, min: 0, max: 1000 },
  ],
  audit: [{ path: "audit.sample_rate", kind: "number", default: 0.1, min: 0, max: 1 }],
};
export const KEYS: readonly KeySpec[] = SECTION_NAMES.flatMap((name) => SECTIONS[name]);

/** `autonomy.REPO_KEYS`. */
export const REPO_KEYS: readonly KeySpec[] = [
  { path: "mode", kind: "choice", default: "off", options: MODES },
  { path: "target_branch", kind: "optional", default: null, maxChars: 255 },
  { path: "prod_release", kind: "choice", default: "immediate", options: RELEASES },
  { path: "after_uat_hours", kind: "number", default: 4, min: 0, max: 720 },
  ...JOBS.map((job): KeySpec => ({ path: `jobs.${job}`, kind: "optional", default: null, maxChars: 255 })),
];

/** A key's words in the messages: its dotted path with underscores (`merge_hold_until`, `jobs_uat_check`). */
export function messageKey(path: string): string {
  return path.replaceAll(".", "_");
}

type Tree = Record<string, unknown>;

function isTree(value: unknown): value is Tree {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The value at a dotted path of `tree`, or undefined when the tree does not set it. */
export function readPath(tree: unknown, path: string): unknown {
  let node: unknown = tree;
  for (const part of path.split(".")) {
    if (!isTree(node) || !(part in node)) return undefined;
    node = node[part];
  }
  return node;
}

function writePath(tree: Tree, path: string, value: unknown): void {
  const parts = path.split(".");
  let node = tree;
  for (const part of parts.slice(0, -1)) {
    if (!isTree(node[part])) node[part] = {};
    node = node[part] as Tree;
  }
  node[parts.at(-1) as string] = value;
}

/** The repos a policy names: those of the project (`effective` lists each) and any its body still names. */
export function policyRepos(effective: unknown): string[] {
  const repos = readPath(effective, "repos");
  return isTree(repos) ? Object.keys(repos).sort((a, b) => a.localeCompare(b)) : [];
}

// The form

export type KeyValues = Record<string, string>;
export type PolicyForm = { global: KeyValues; repos: Record<string, KeyValues>; note: string };

/** A value as its field holds it. */
export function fieldText(spec: KeySpec, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (spec.kind === "globs") return Array.isArray(value) ? value.join("\n") : "";
  return String(value);
}

/**
 * The form of a policy: each key at the value the policy gives it. A repo's target branch shows what the body sets,
 * empty for the repo's default branch, which its field names as a placeholder.
 */
export function policyForm(policy: { policy: Tree; effective: Tree }): PolicyForm {
  const global: KeyValues = {};
  for (const spec of KEYS) global[spec.path] = fieldText(spec, readPath(policy.effective, spec.path) ?? spec.default);
  const repos: Record<string, KeyValues> = {};
  for (const repo of policyRepos(policy.effective)) {
    const values: KeyValues = {};
    for (const spec of REPO_KEYS) {
      const from = spec.path === "target_branch" ? policy.policy : policy.effective;
      const value = readPath(from, `repos.${repo}.${spec.path}`);
      values[spec.path] = fieldText(spec, value === undefined ? spec.default : value);
    }
    repos[repo] = values;
  }
  return { global, repos, note: "" };
}

// Checks

export type Problem =
  | { code: "oneOf"; options: readonly string[] }
  | { code: "whole"; min: number; max: number }
  | { code: "number"; min: number; max: number }
  | { code: "aboveNumber"; min: number; max: number }
  | { code: "line"; max: number }
  | { code: "globs"; max: number; chars: number }
  | { code: "github" };

export type PolicyProblems = { global: Record<string, Problem>; repos: Record<string, Record<string, Problem>>; note?: Problem };

const NUMBER = /^-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$/;
const WHOLE = /^-?\d+$/;
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

/** A line `_line` of the hub takes: printable, one line, no space at either end. */
function goodLine(text: string, max: number): boolean {
  return text.length > 0 && text.length <= max && !CONTROL.test(text) && text === text.trim();
}

/** The value a field gives its key, or a problem. Text fields are trimmed first. */
export function parseField(spec: KeySpec, raw: string): { value: unknown } | { problem: Problem } {
  const text = spec.kind === "globs" ? raw : raw.trim();
  switch (spec.kind) {
    case "choice":
      return spec.options?.includes(text) ? { value: text } : { problem: { code: "oneOf", options: spec.options ?? [] } };
    case "whole": {
      const value = Number(text);
      const right = WHOLE.test(text) && value >= (spec.min ?? 0) && value <= (spec.max ?? 0);
      return right ? { value } : { problem: { code: "whole", min: spec.min ?? 0, max: spec.max ?? 0 } };
    }
    case "number": {
      const value = Number(text);
      const low = spec.min ?? 0;
      const right = NUMBER.test(text) && Number.isFinite(value) && (spec.aboveMin ? value > low : value >= low) && value <= (spec.max ?? 0);
      if (right) return { value };
      return { problem: { code: spec.aboveMin ? "aboveNumber" : "number", min: low, max: spec.max ?? 0 } };
    }
    case "line":
      return goodLine(text, spec.maxChars ?? 255) ? { value: text } : { problem: { code: "line", max: spec.maxChars ?? 255 } };
    case "optional":
      if (text === "") return { value: null };
      return goodLine(text, spec.maxChars ?? 255) ? { value: text } : { problem: { code: "line", max: spec.maxChars ?? 255 } };
    case "globs": {
      const globs = text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      const right = globs.length <= MAX_RISK_PATHS && globs.every((glob) => goodLine(glob, MAX_GLOB_CHARS));
      return right ? { value: globs } : { problem: { code: "globs", max: MAX_RISK_PATHS, chars: MAX_GLOB_CHARS } };
    }
  }
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Whether the body keeps a key: the policy set it and the admin left it, or its value is not the default. */
function kept(spec: KeySpec, value: unknown, held: unknown): boolean {
  if (held !== undefined && same(value, held)) return true;
  return !same(value, spec.default);
}

export type RepoFacts = { origin: string | null; listed: boolean };

/** Whether a repo lives on github.com, where the Curator's tiers decide what the hub merges: auto is refused there. */
export function onGithub(origin: string | null | undefined): boolean {
  if (!origin) return false;
  try {
    return new URL(origin.replace(/^git@([^:]+):/, "https://$1/")).hostname.toLowerCase() === "github.com";
  } catch {
    return false;
  }
}

export type PolicyResult = { body: Tree; problems: null } | { body: null; problems: PolicyProblems };

/**
 * The body the form makes of `held` (the policy's newest body), or the problems of its fields. A repo the project no
 * longer lists is dropped, as the hub would refuse it; one on github.com may not be set to auto.
 */
export function policyBody(form: PolicyForm, held: Tree, repos: Record<string, RepoFacts>): PolicyResult {
  const problems: PolicyProblems = { global: {}, repos: {} };
  const body: Tree = {};
  for (const spec of KEYS) {
    const parsed = parseField(spec, form.global[spec.path] ?? "");
    if ("problem" in parsed) problems.global[spec.path] = parsed.problem;
    else if (kept(spec, parsed.value, readPath(held, spec.path))) writePath(body, spec.path, parsed.value);
  }
  for (const [repo, values] of Object.entries(form.repos)) {
    if (repos[repo] && !repos[repo].listed) continue;
    const own: Tree = {};
    const found: Record<string, Problem> = {};
    for (const spec of REPO_KEYS) {
      const parsed = parseField(spec, values[spec.path] ?? "");
      if ("problem" in parsed) found[spec.path] = parsed.problem;
      else if (kept(spec, parsed.value, readPath(held, `repos.${repo}.${spec.path}`))) writePath(own, spec.path, parsed.value);
    }
    if (values.mode === "auto" && onGithub(repos[repo]?.origin)) found.mode = { code: "github" };
    if (Object.keys(found).length > 0) problems.repos[repo] = found;
    else if (Object.keys(own).length > 0) {
      if (!isTree(body.repos)) body.repos = {};
      (body.repos as Tree)[repo] = own;
    }
  }
  if (form.note.length > MAX_NOTE_CHARS) problems.note = { code: "line", max: MAX_NOTE_CHARS };
  const wrong = Object.keys(problems.global).length + Object.keys(problems.repos).length + (problems.note ? 1 : 0);
  return wrong > 0 ? { body: null, problems } : { body, problems: null };
}

/** How many fields are wrong. */
export function problemCount(problems: PolicyProblems): number {
  const repos = Object.values(problems.repos).reduce((sum, found) => sum + Object.keys(found).length, 0);
  return Object.keys(problems.global).length + repos + (problems.note ? 1 : 0);
}

/** The repo key of a dotted policy key (`repos.<repo>.jobs.uat_check`), read from its known end: a repo's name may hold a dot. */
export function splitKey(key: string): { repo: string | null; path: string } {
  if (!key.startsWith("repos.")) return { repo: null, path: key };
  const rest = key.slice("repos.".length);
  for (const spec of REPO_KEYS) {
    if (rest.endsWith(`.${spec.path}`)) return { repo: rest.slice(0, -(spec.path.length + 1)), path: spec.path };
  }
  return { repo: null, path: key };
}

/** The spec of a dotted key, a repo's or the policy's own; null for a key this web does not know. */
export function specOf(key: string): KeySpec | null {
  const { repo, path } = splitKey(key);
  return (repo === null ? KEYS : REPO_KEYS).find((spec) => spec.path === path) ?? null;
}

/** Whether `value` is the default of `spec`, so a view can say "default" beside it. */
export function isDefault(spec: KeySpec, value: unknown): boolean {
  return same(value ?? null, spec.default);
}

// The page's state in its URL

export type PolicyView = { revision: number | null; edit: boolean; from: number | null; to: number | null };

type Params = { get(name: string): string | null } | Record<string, string | string[] | undefined>;

function param(params: Params, name: string): string | null {
  if ("get" in params && typeof params.get === "function") return params.get(name);
  const value = (params as Record<string, string | string[] | undefined>)[name];
  return (Array.isArray(value) ? value[0] : value) ?? null;
}

function count(value: string | null, min: number): number | null {
  if (value === null || !/^\d{1,9}$/.test(value)) return null;
  const number = Number(value);
  return number >= min ? number : null;
}

/** `?revision=N` (an older revision), `?edit=1` (the form), `?from=N&to=M` (the comparison). */
export function readPolicyView(params: Params): PolicyView {
  return {
    revision: count(param(params, "revision"), 1),
    edit: param(params, "edit") === "1",
    from: count(param(params, "from"), 0),
    to: count(param(params, "to"), 0),
  };
}
