import type { PlanSummary } from "@/lib/plans";

/**
 * The plans list of a project as one list (web/DESIGN.md, Plans): which plans it shows, in what order and under which
 * headings, from the filters in the page's address. No React and no DOM here, so every rule is tested in vitest.
 */

export const PLAN_VIEWS = ["open", "completed"] as const;
export type PlanListView = (typeof PLAN_VIEWS)[number];
export const PLAN_GROUPS = ["none", "problem", "owner", "repo"] as const;
export type PlanGroupBy = (typeof PLAN_GROUPS)[number];
export const PLAN_SORTS = ["updated", "progress", "name"] as const;
export type PlanSort = (typeof PLAN_SORTS)[number];

export type PlanListFilters = {
  view: PlanListView;
  q: string;
  owner: string | null;
  problem: string | null;
  repo: string | null;
  group: PlanGroupBy;
  sort: PlanSort;
};

export const DEFAULT_FILTERS: PlanListFilters = {
  view: "open",
  q: "",
  owner: null,
  problem: null,
  repo: null,
  group: "none",
  sort: "updated",
};

const MAX_QUERY = 200;
const MAX_VALUE = 200;

function oneOf<T extends string>(values: readonly T[], value: string | null, fallback: T): T {
  return value !== null && (values as readonly string[]).includes(value) ? (value as T) : fallback;
}

function value(params: URLSearchParams, name: string): string | null {
  const raw = (params.get(name) ?? "").trim();
  return raw ? raw.slice(0, MAX_VALUE) : null;
}

/** The filters an address names (`?view=completed&q=kg&repo=lumen-api&group=repo&sort=name`); junk reads as the default. */
export function readFilters(params: URLSearchParams): PlanListFilters {
  return {
    view: oneOf(PLAN_VIEWS, params.get("view"), DEFAULT_FILTERS.view),
    q: (params.get("q") ?? "").trim().slice(0, MAX_QUERY),
    owner: value(params, "owner"),
    problem: value(params, "problem"),
    repo: value(params, "repo"),
    group: oneOf(PLAN_GROUPS, params.get("group"), DEFAULT_FILTERS.group),
    sort: oneOf(PLAN_SORTS, params.get("sort"), DEFAULT_FILTERS.sort),
  };
}

/** The address of `filters`, without the defaults, so the plain page stays `/plans`. */
export function writeFilters(filters: PlanListFilters): string {
  const params = new URLSearchParams();
  for (const name of ["view", "q", "owner", "problem", "repo", "group", "sort"] as const) {
    const current = filters[name];
    if (current && current !== DEFAULT_FILTERS[name]) params.set(name, current);
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** How many filters narrow the list (owner, problem, repo), for the phone's "Filters (n)". */
export function activeFilters(filters: PlanListFilters): number {
  return [filters.owner, filters.problem, filters.repo].filter(Boolean).length;
}

const NAME_LENGTH = 120;

/** The first sentence of a goal, on one line, cut to 120 characters. */
export function firstSentence(goal: string | null | undefined): string | null {
  const line = (goal ?? "").replace(/\s+/g, " ").trim();
  if (!line) return null;
  const end = line.search(/[.!?](\s|$)/);
  const sentence = end >= 0 ? line.slice(0, end + 1) : line;
  return sentence.length > NAME_LENGTH ? `${sentence.slice(0, NAME_LENGTH - 1).trimEnd()}…` : sentence;
}

/** What a row calls the plan: its title, else the start of its goal, else its id. */
export function planName(plan: Pick<PlanSummary, "plan_id" | "title" | "goal">): string {
  return plan.title?.trim() || firstSentence(plan.goal) || plan.plan_id;
}

/** Every step of the plan is done or descoped: the row is drawn faint. A plan without steps is not finished. */
export function planFinished(plan: Pick<PlanSummary, "steps_total" | "steps_done">): boolean {
  return plan.steps_total > 0 && plan.steps_done >= plan.steps_total;
}

/** The marks of the step strip, one per step in the plan's order. */
export const STRIP_STATUSES = ["done", "in_progress", "blocked", "pending", "descoped", "other"] as const;
export type StripStatus = (typeof STRIP_STATUSES)[number];

/** A step's mark: its status when the strip knows it, pending for none, other for a value the schema lacks. */
export function stripStatus(status: string | null | undefined): StripStatus {
  if (status === null || status === undefined || status === "") return "pending";
  return (STRIP_STATUSES as readonly string[]).includes(status) ? (status as StripStatus) : "other";
}

/** Where the current step stands in the plan's order (1 for the first), or null when the list does not hold it. */
export function currentPosition(plan: Pick<PlanSummary, "steps" | "current_step">): number | null {
  const key = plan.current_step?.key;
  if (key === undefined) return null;
  const index = (plan.steps ?? []).findIndex((step) => step.key === key);
  return index >= 0 ? index + 1 : null;
}

/** The plan matches the search when its title, goal or id holds the text, case aside. */
export function matchesQuery(plan: Pick<PlanSummary, "plan_id" | "title" | "goal">, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase("vi");
  if (!needle) return true;
  return [plan.plan_id, plan.title ?? "", plan.goal ?? ""].some((text) => text.toLocaleLowerCase("vi").includes(needle));
}

/** The plans of the view that pass the search and the Owner, Problem and Repo filters. */
export function filterPlans(plans: readonly PlanSummary[], filters: PlanListFilters): PlanSummary[] {
  const area = filters.view === "open" ? "active" : "completed";
  return plans.filter(
    (plan) =>
      plan.area === area &&
      matchesQuery(plan, filters.q) &&
      (filters.owner === null || plan.updated_by === filters.owner) &&
      (filters.problem === null || (plan.problems ?? []).includes(filters.problem)) &&
      (filters.repo === null || (plan.repos ?? []).includes(filters.repo)),
  );
}

function progress(plan: PlanSummary): number {
  return plan.steps_total > 0 ? plan.steps_done / plan.steps_total : 0;
}

const byUpdated = (a: PlanSummary, b: PlanSummary) => Date.parse(b.updated_at) - Date.parse(a.updated_at);
const byName = (a: PlanSummary, b: PlanSummary) => planName(a).localeCompare(planName(b), "vi", { sensitivity: "base" });

/**
 * The plans in the order picked: last changed first (the default); furthest along first, then last changed; or by
 * name. Ties fall back to the plan's id, so the order never jumps between two reads.
 */
export function sortPlans(plans: readonly PlanSummary[], sort: PlanSort): PlanSummary[] {
  const compare =
    sort === "name" ? byName : sort === "progress" ? (a: PlanSummary, b: PlanSummary) => progress(b) - progress(a) || byUpdated(a, b) : byUpdated;
  return [...plans].sort((a, b) => compare(a, b) || a.plan_id.localeCompare(b.plan_id));
}

/** The values a plan has for a grouping: its problems, its last editor or its repos. */
export function groupValues(plan: PlanSummary, group: Exclude<PlanGroupBy, "none">): string[] {
  if (group === "owner") return [plan.updated_by];
  return [...((group === "problem" ? plan.problems : plan.repos) ?? [])];
}

export type PlanGroup = { key: string | null; plans: PlanSummary[] };

/**
 * The sorted plans under a heading per value, the headings in order (problem codes by number) and the plans with no
 * value last (key null). A plan with two problems or two repos shows under each. `none` is one group of every plan.
 */
export function groupPlans(plans: readonly PlanSummary[], group: PlanGroupBy): PlanGroup[] {
  if (group === "none") return [{ key: null, plans: [...plans] }];
  const groups = new Map<string, PlanSummary[]>();
  const without: PlanSummary[] = [];
  for (const plan of plans) {
    const values = groupValues(plan, group);
    if (values.length === 0) without.push(plan);
    for (const key of values) groups.set(key, [...(groups.get(key) ?? []), plan]);
  }
  const keys = [...groups.keys()].sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  const listed: PlanGroup[] = keys.map((key) => ({ key, plans: groups.get(key)! }));
  return without.length > 0 ? [...listed, { key: null, plans: without }] : listed;
}

/** The choices of the Owner, Problem and Repo filters: the values the plans of the view hold, in order. */
export function filterChoices(plans: readonly PlanSummary[]): { owners: string[]; problems: string[]; repos: string[] } {
  const sorted = (values: Iterable<string>) => [...new Set(values)].sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  return {
    owners: sorted(plans.map((plan) => plan.updated_by)),
    problems: sorted(plans.flatMap((plan) => plan.problems ?? [])),
    repos: sorted(plans.flatMap((plan) => plan.repos ?? [])),
  };
}

/** A day of the calendar of `timeZone` as `YYYY-MM-DD`. */
export function calendarDay(at: Date | string | number, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(
    new Date(at),
  );
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/**
 * How a row writes when the plan last changed, against `today` of the page's time zone: the time for today,
 * Yesterday, the weekday within the week, then the date (with its year when it is not this one).
 */
export type ChangeKind = "time" | "yesterday" | "weekday" | "date" | "dateYear";

export function changeKind(day: string, today: string): ChangeKind {
  const before = Math.round((Date.parse(`${today}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86_400_000);
  if (before <= 0) return "time";
  if (before === 1) return "yesterday";
  if (before < 7) return "weekday";
  return day.slice(0, 4) === today.slice(0, 4) ? "date" : "dateYear";
}
