import type { components } from "@/lib/api/schema";

/**
 * Plans as the web reads them. The API sends a plan's body as the YAML file reads (plan.schema.json, loosely: a
 * plan may carry keys the schema only warns about), so every field is narrowed here from `unknown` before a page
 * shows it. Nothing in this module changes a plan; the web only reads them.
 */
type Schemas = components["schemas"];
export type PlanSummary = Schemas["PlanSummary"];
export type Plan = Schemas["Plan"];
export type PlanRevision = Schemas["Revision"];
export type PlanDiff = Schemas["PlanDiff"];
export type PlanDiffHunk = Schemas["PlanDiffHunk"];
export type PlanDiffLine = Schemas["PlanDiffLine"];
export type PlanArea = PlanSummary["area"];

/** `plans.PLAN_ID` of the API: anything else is not a plan. */
export const PLAN_ID = /^[a-z0-9][a-z0-9-]{0,99}$/;

/** The statuses of a step, in the order the board shows them (plan.schema.json's enum). */
export const STEP_STATUSES = ["pending", "in_progress", "blocked", "done"] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];
/** A step whose status is none of the four (a typo, or a value from an older plan) is grouped as "other". */
export type StepGroup = StepStatus | "other";
export const STEP_GROUPS: readonly StepGroup[] = [...STEP_STATUSES, "other"];

export const REPO_STATUSES = ["merged", "done", "in_progress", "pending", "not-needed"] as const;
export type RepoStatus = (typeof REPO_STATUSES)[number];

export type PlanStep = {
  /** How plans name a step (evo-cli's `step_key`): its id, else its order, else its position. */
  key: string;
  index: number;
  title: string | null;
  repo: string | null;
  what: string | null;
  verify: string | null;
  note: string | null;
  evidence: string | null;
  doneAt: string | null;
  group: StepGroup;
  /** The status as written, when it is not one of the four (or null when the step has none). */
  rawStatus: string | null;
  blocking: boolean | null;
  /** The keys of the steps this one waits for, as text (5 and "5" are the same step). */
  dependsOn: string[];
  /** Keys the page has no place for (why, scope, acceptance, ...), alphabetically (jsonb keeps no key order). */
  extra: [string, unknown][];
};

export type PlanRepo = {
  repo: string;
  branch: string | null;
  order: string | null;
  status: string | null;
  dependsOn: string[];
  scope: string | null;
  mergedInto: string | null;
  mergedAt: string | null;
  doneAt: string | null;
  pr: string | null;
  note: string | null;
};

export type PlanView = {
  id: string;
  title: string | null;
  goal: string | null;
  context: string | null;
  status: string | null;
  createdAt: string | null;
  steps: PlanStep[];
  repos: PlanRepo[];
  /** Every other top-level key (acceptance, risks, decisions, ...); the page puts them in order. */
  sections: [string, unknown][];
};

/** The sections of a plan the plan page lays out itself; the rest are shown as they are. */
const SHOWN = new Set(["id", "title", "goal", "context", "status", "created_at", "steps", "repos", "hub"]);
const STEP_SHOWN = new Set([
  "id",
  "order",
  "title",
  "repo",
  "what",
  "verify",
  "note",
  "evidence",
  "done_at",
  "status",
  "blocking",
  "depends_on",
]);

export type JsonObject = { [key: string]: unknown };

export function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A scalar as text: strings as they are, numbers and booleans written out, anything else null. */
export function text(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function keys(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(text).filter((key): key is string => key !== null);
}

function isStepStatus(value: string | null): value is StepStatus {
  return value !== null && (STEP_STATUSES as readonly string[]).includes(value);
}

/** evo-cli's `step_key`: the id, else the order, else the position. */
export function stepKey(step: unknown, index: number): string {
  if (isObject(step)) {
    for (const name of ["id", "order"]) {
      const found = text(step[name]);
      if (found !== null) return found;
    }
  }
  return String(index);
}

export function parseStep(step: unknown, index: number): PlanStep {
  const data = isObject(step) ? step : { what: text(step) };
  const status = text(data.status);
  return {
    key: stepKey(step, index),
    index,
    title: text(data.title),
    repo: text(data.repo),
    what: text(data.what),
    verify: text(data.verify),
    note: text(data.note),
    evidence: text(data.evidence),
    doneAt: text(data.done_at),
    group: status === null ? "pending" : isStepStatus(status) ? status : "other",
    rawStatus: isStepStatus(status) ? null : status,
    blocking: typeof data.blocking === "boolean" ? data.blocking : null,
    dependsOn: keys(data.depends_on),
    extra: Object.entries(data)
      .filter(([key]) => !STEP_SHOWN.has(key))
      .sort(([a], [b]) => a.localeCompare(b)),
  };
}

function parseRepo(item: unknown): PlanRepo | null {
  if (!isObject(item)) {
    const name = text(item);
    return name === null
      ? null
      : {
          repo: name,
          branch: null,
          order: null,
          status: null,
          dependsOn: [],
          scope: null,
          mergedInto: null,
          mergedAt: null,
          doneAt: null,
          pr: null,
          note: null,
        };
  }
  const repo = text(item.repo);
  if (repo === null) return null;
  return {
    repo,
    branch: text(item.branch),
    order: text(item.order),
    status: text(item.status),
    dependsOn: keys(item.depends_on),
    scope: text(item.scope),
    mergedInto: text(item.merged_into),
    mergedAt: text(item.merged_at),
    doneAt: text(item.done_at),
    pr: text(item.pr),
    note: text(item.note),
  };
}

export function parsePlan(body: JsonObject, fallbackId = ""): PlanView {
  const steps = Array.isArray(body.steps) ? body.steps : [];
  const repos = Array.isArray(body.repos) ? body.repos : [];
  return {
    id: text(body.id) ?? fallbackId,
    title: text(body.title),
    goal: text(body.goal),
    context: text(body.context),
    status: text(body.status),
    createdAt: text(body.created_at),
    steps: steps.map(parseStep),
    repos: repos.map(parseRepo).filter((repo): repo is PlanRepo => repo !== null),
    sections: Object.entries(body).filter(([key, value]) => !SHOWN.has(key) && value !== null && value !== undefined),
  };
}

export type StepCounts = Record<StepGroup, number> & { total: number };

export function countSteps(steps: readonly Pick<PlanStep, "group">[]): StepCounts {
  const counts: StepCounts = { pending: 0, in_progress: 0, blocked: 0, done: 0, other: 0, total: steps.length };
  for (const step of steps) counts[step.group] += 1;
  return counts;
}

/** Whole percent of `done` out of `total`, rounded down so a plan shows 100 % only when every step is done. */
export function percent(done: number, total: number): number {
  if (total <= 0) return 0;
  return Math.floor((done / total) * 100);
}

/** The steps that name `key` in their depends_on: what waits for this step. */
export function dependents(steps: readonly PlanStep[], key: string): PlanStep[] {
  return steps.filter((step) => step.dependsOn.includes(key));
}

/** A step's name as people say it: "26" and its title. */
export function stepLabel(step: Pick<PlanStep, "key" | "title" | "what">): string {
  const name = step.title ?? step.what?.split("\n")[0] ?? "";
  return name.length > 120 ? `${name.slice(0, 119)}…` : name;
}

/** Steps matching a search (id, title, repo, what) and a repo filter ("" for every repo). */
export function filterSteps(steps: readonly PlanStep[], query: string, repo: string): PlanStep[] {
  const needle = query.trim().toLocaleLowerCase("vi");
  return steps.filter((step) => {
    if (repo && step.repo !== repo) return false;
    if (!needle) return true;
    return [step.key, step.title, step.repo, step.what].some(
      (value) => value !== null && value.toLocaleLowerCase("vi").includes(needle),
    );
  });
}

/** The repos the steps name, sorted, for the board's filter. */
export function stepRepos(steps: readonly PlanStep[]): string[] {
  return [...new Set(steps.map((step) => step.repo).filter((repo): repo is string => repo !== null))].sort();
}

// Revisions and their diff

/** The pair of revisions to compare: the ones asked for when both exist, else the latest against the one before. */
export function comparedPair(
  revisions: readonly Pick<PlanRevision, "revision">[],
  from: number | null,
  to: number | null,
): { from: number; to: number } | null {
  const numbers = revisions.map((r) => r.revision).sort((a, b) => a - b);
  if (numbers.length === 0) return null;
  if (from !== null && to !== null && numbers.includes(from) && numbers.includes(to)) return { from, to };
  if (numbers.length === 1) return { from: numbers[0], to: numbers[0] };
  return { from: numbers[numbers.length - 2], to: numbers[numbers.length - 1] };
}

/** The revision before `revision` among those the caller sees, or null for the first. */
export function previousRevision(revisions: readonly Pick<PlanRevision, "revision">[], revision: number): number | null {
  const earlier = revisions.map((r) => r.revision).filter((r) => r < revision);
  return earlier.length ? Math.max(...earlier) : null;
}

/** A positive integer from a search parameter, else null. */
export function revisionParam(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value;
  if (!raw || !/^[1-9]\d{0,8}$/.test(raw)) return null;
  return Number(raw);
}

export type SplitRow =
  | { kind: "context"; left: PlanDiffLine; right: PlanDiffLine }
  | { kind: "change"; left: PlanDiffLine | null; right: PlanDiffLine | null };

/** A hunk as side-by-side rows: unchanged lines on both sides, each run of removed lines beside the added run
 * that follows it, paired line by line, with an empty cell where one run is longer. */
export function splitRows(hunk: Pick<PlanDiffHunk, "lines">): SplitRow[] {
  const rows: SplitRow[] = [];
  let removed: PlanDiffLine[] = [];
  let added: PlanDiffLine[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(removed.length, added.length); i += 1) {
      rows.push({ kind: "change", left: removed[i] ?? null, right: added[i] ?? null });
    }
    removed = [];
    added = [];
  };
  for (const line of hunk.lines) {
    if (line.kind === "context") {
      flush();
      rows.push({ kind: "context", left: line, right: line });
    } else if (line.kind === "removed") {
      if (added.length) flush();
      removed.push(line);
    } else {
      added.push(line);
    }
  }
  flush();
  return rows;
}
