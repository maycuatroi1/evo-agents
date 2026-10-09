import { canRerun, type Overview, type OverviewRun } from "@/components/home/model";
import type { PlanSummary } from "@/lib/plans";

/**
 * What the command palette offers and finds (web/DESIGN.md, Command palette). Pure functions, shared by the palette and
 * its tests: how a query matches, the scopes Tab moves through, which actions a member's grants allow, which failed
 * run Rerun names, and the runs the overview holds.
 */

/** A grant as whoami lists it. */
export type Grant = { project: string; role: string };

/** The project the palette looks in, or null for every project of the visitor's grants. */
export type Scope = string | null;

/** How many items a group shows: a few while nothing is typed, more once a query narrows them. */
export const IDLE_LIMIT = 5;
export const QUERY_LIMIT = 8;
/** Run plan items while nothing is typed: the first active plans with steps left. */
export const IDLE_PLAN_RUNS = 3;
/** The runs one search of a project's list asks for. */
export const RUN_SEARCH_LIMIT = QUERY_LIMIT;
/** How long typing pauses before the palette asks the hub (ms). */
export const SEARCH_DEBOUNCE_MS = 150;
/** The projects whose plans the palette reads at once when it looks in every project. */
export const MAX_SCOPE_PROJECTS = 20;

/** Lower case without diacritics, so "phat hanh" finds "Phát hành" and "dong" finds "Đồng". */
export function fold(text: string): string {
  return text.toLocaleLowerCase().normalize("NFD").replace(/\p{M}/gu, "").replace(/đ/g, "d");
}

/** The words of a query, folded. */
export function queryWords(query: string): string[] {
  return fold(query).split(/\s+/).filter(Boolean);
}

/** Whether `haystack` holds every word of the query; no word matches everything. */
export function matchesWords(words: readonly string[], haystack: string): boolean {
  if (words.length === 0) return true;
  const text = fold(haystack);
  return words.every((word) => text.includes(word));
}

/** The query as the runs list's `q` takes it: trimmed, at most 200 characters; empty for none. */
export function runSearchText(query: string): string {
  return query.trim().slice(0, 200);
}

export function canWrite(role: string | null | undefined): boolean {
  return role === "writer" || role === "admin";
}

export function roleIn(grants: readonly Grant[], project: string): string | null {
  return grants.find((grant) => grant.project === project)?.role ?? null;
}

/** The projects of the grants, by name: the scopes Tab moves through before "every project". */
export function grantedProjects(grants: readonly Grant[]): string[] {
  return [...new Set(grants.map((grant) => grant.project))].sort((a, b) => a.localeCompare(b));
}

/** Where the palette starts: the page's project when the visitor holds a grant on it, else every project. */
export function initialScope(current: string | null, projects: readonly string[]): Scope {
  return current !== null && projects.includes(current) ? current : null;
}

/** The scopes in Tab order: each project of the grants, then every project. */
export function scopeCycle(projects: readonly string[]): Scope[] {
  return [...projects, null];
}

/** The scope after `scope` (Tab) or before it (Shift Tab), wrapping around. */
export function stepScope(cycle: readonly Scope[], scope: Scope, step: 1 | -1): Scope {
  if (cycle.length === 0) return null;
  const index = cycle.indexOf(scope);
  const from = index < 0 ? (step === 1 ? -1 : 0) : index;
  return cycle[(from + step + cycle.length) % cycle.length];
}

/** The projects a scope covers: the one it names, or every project of the grants (at most 20). */
export function scopeProjects(scope: Scope, projects: readonly string[]): string[] {
  return scope === null ? projects.slice(0, MAX_SCOPE_PROJECTS) : [scope];
}

/** A plan Run plan may start: active, with steps not done, in a project where the visitor writes. */
export type PlanRunOffer = { project: string; planId: string; title: string; left: number };

export function planRunOffers(plans: readonly { project: string; plans: readonly PlanSummary[] }[], grants: readonly Grant[]): PlanRunOffer[] {
  return plans.flatMap(({ project, plans: list }) =>
    canWrite(roleIn(grants, project))
      ? list
          .filter((plan) => plan.area === "active" && plan.steps_done < plan.steps_total)
          .map((plan) => ({ project, planId: plan.plan_id, title: plan.title ?? plan.plan_id, left: plan.steps_total - plan.steps_done }))
      : [],
  );
}

/** A plan Revise with agent may start an author run on: active, in a project where the visitor writes. */
export type ReviseOffer = { project: string; planId: string; title: string };

export function reviseOffers(plans: readonly { project: string; plans: readonly PlanSummary[] }[], grants: readonly Grant[]): ReviseOffer[] {
  return plans.flatMap(({ project, plans: list }) =>
    canWrite(roleIn(grants, project))
      ? list.filter((plan) => plan.area === "active").map((plan) => ({ project, planId: plan.plan_id, title: plan.title ?? plan.plan_id }))
      : [],
  );
}

/** The projects of a scope where the visitor may dispatch a step (and write a plan with New plan). */
export function dispatchProjects(projects: readonly string[], grants: readonly Grant[]): string[] {
  return projects.filter((project) => canWrite(roleIn(grants, project)));
}

/** Whether the visitor may register a worker: the writer role on at least one project (docs/workers.md). */
export function canRegister(grants: readonly Grant[]): boolean {
  return grants.some((grant) => canWrite(grant.role));
}

function sameStep(a: OverviewRun, b: OverviewRun): boolean {
  return a.project === b.project && a.plan_id === b.plan_id && a.step_key === b.step_key;
}

/**
 * The run Rerun names: the visitor's latest failed or lost run of one step in the scope, where they write (Home's
 * rule), unless a later run of the same step is in the overview already, since that one is the step's rerun.
 */
export function rerunCandidate(overview: Pick<Overview, "recent_runs" | "active_runs" | "projects">, viewer: string | null, scope: Scope): OverviewRun | null {
  const later = [...overview.active_runs, ...overview.recent_runs];
  return (
    overview.recent_runs.find(
      (run) =>
        (scope === null || run.project === scope) &&
        canRerun(run, viewer, overview.projects) &&
        !later.some((other) => other.id > run.id && sameStep(other, run)),
    ) ?? null
  );
}

/** The overview's runs in a scope: those in flight first (as the overview orders them), then the ones that ended last. */
export function overviewRuns(overview: Pick<Overview, "active_runs" | "recent_runs"> | undefined, scope: Scope): OverviewRun[] {
  if (!overview) return [];
  const seen = new Set<string>();
  return [...overview.active_runs, ...overview.recent_runs].filter((run) => {
    const key = `${run.project}#${run.id}`;
    if (seen.has(key) || (scope !== null && run.project !== scope)) return false;
    seen.add(key);
    return true;
  });
}

/** What a run is found by: its number as #N, its title, plan, step, project, worker and owner. */
export function runHaystack(run: {
  id: number;
  title: string | null;
  plan_id: string;
  step_key: string | null;
  project: string;
  dispatched_by: string;
  plan_title?: string | null;
  worker?: string | null;
}): string {
  return [`#${run.id}`, run.title, run.plan_title, run.plan_id, run.step_key, run.project, run.worker, run.dispatched_by]
    .filter(Boolean)
    .join(" ");
}

/** Plans in the order the palette lists them: active first, then by the last change, newest first. */
export function orderPlans<T extends Pick<PlanSummary, "area" | "updated_at">>(plans: readonly T[]): T[] {
  return [...plans].sort((a, b) => (a.area === b.area ? b.updated_at.localeCompare(a.updated_at) : a.area === "active" ? -1 : 1));
}
