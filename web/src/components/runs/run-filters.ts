import { HELD_STATES, MAX_QUERY, type RunQuery, RUNS_PAGE, type RunState, type StateCounts } from "./queries";

/**
 * The runs list's facets and the filters it keeps in the URL, read by the runs page on the server and in the browser.
 * Pure functions, shared by the page and its tests.
 */

// The list's facets, kept in the URL. Each one is a set of states the API filters by (?state= repeated).

export const RUN_FACETS = ["active", "review", "done", "ended"] as const;
export type RunFacet = (typeof RUN_FACETS)[number];

export const FACET_STATES: Record<RunFacet, readonly RunState[]> = {
  active: ["queued", ...HELD_STATES, "parked"],
  review: ["review"],
  done: ["done"],
  ended: ["failed", "lost", "cancelled"],
};

export function isRunFacet(value: string | null | undefined): value is RunFacet {
  return (RUN_FACETS as readonly string[]).includes(value ?? "");
}

export type RunFilters = {
  /** null: every state. */
  facet: RunFacet | null;
  q: string;
  /** 1-based. */
  page: number;
};

export const NO_FILTERS: RunFilters = { facet: null, q: "", page: 1 };
/** The API pages up to offset 100 000. */
export const MAX_PAGE = Math.floor(100_000 / RUNS_PAGE) + 1;

type Params = { get(name: string): string | null };

export function readFilters(source: Params): RunFilters {
  const facet = source.get("state");
  const page = Number(source.get("page") ?? "1");
  return {
    facet: isRunFacet(facet) ? facet : null,
    q: (source.get("q") ?? "").trim().slice(0, MAX_QUERY),
    page: Number.isInteger(page) && page >= 1 && page <= MAX_PAGE ? page : 1,
  };
}

export function filtersSearch(filters: RunFilters): string {
  const params = new URLSearchParams();
  if (filters.facet) params.set("state", filters.facet);
  if (filters.q) params.set("q", filters.q);
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function isFiltered(filters: RunFilters): boolean {
  return filters.facet !== null || filters.q !== "";
}

/** What the API is asked for the list a set of filters shows. */
export function listQuery(filters: RunFilters): RunQuery {
  return {
    states: filters.facet ? FACET_STATES[filters.facet] : [],
    q: filters.q,
    limit: RUNS_PAGE,
    offset: (filters.page - 1) * RUNS_PAGE,
  };
}

/** Runs a facet would show, from the counts of every state (null: all of them). */
export function facetCount(counts: StateCounts, facet: RunFacet | null): number {
  const states = facet ? FACET_STATES[facet] : (Object.keys(counts) as RunState[]);
  return states.reduce((sum, state) => sum + (counts[state] ?? 0), 0);
}
