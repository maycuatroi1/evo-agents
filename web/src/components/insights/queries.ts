import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { runKeys } from "@/components/runs/queries";
import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Insights page reads: GET /v1/projects/{p}/runs/stats, the runs of the plans the visitor may read that ended
 * on each of the last N days in UTC (7 to 90, 30 without ?days). A member without a grant gets 404, a hub admin
 * without one 403, as for the runs list.
 */
type Schemas = components["schemas"];
export type RunStats = Schemas["RunStats"];
export type RunDay = Schemas["RunDay"];
export type RunFigures = Schemas["RunFigures"];

/** The ranges the page offers, in days; the API takes any number from 7 to 90. */
export const INSIGHT_RANGES = [7, 30, 90] as const;
export type InsightRange = (typeof INSIGHT_RANGES)[number];
/** The API's own default, so the page without ?days and the API without it count the same days. */
export const DEFAULT_RANGE: InsightRange = 30;

/** The range a URL's `days` names; anything the page does not offer is the default. */
export function readRange(value: string | string[] | null | undefined): InsightRange {
  const text = Array.isArray(value) ? value[0] : value;
  const days = Number(text);
  return (INSIGHT_RANGES as readonly number[]).includes(days) ? (days as InsightRange) : DEFAULT_RANGE;
}

/** The query string of a range: none for the default, so the plain URL is the 30 days. */
export function rangeSearch(range: InsightRange): string {
  return range === DEFAULT_RANGE ? "" : `?days=${range}`;
}

/** Below the project's run keys, so a dispatch, a rerun or a cancel elsewhere reloads the figures too. */
export const statsKey = (project: string, range: InsightRange) => [...runKeys.all(project), "stats", range] as const;

/**
 * The project's runs by day over `range` days. The previous range stays on screen while the next loads, so the charts
 * keep their frame instead of flashing a skeleton.
 */
export const runStatsQuery = (api: ApiSource, project: string, range: InsightRange) =>
  queryOptions({
    queryKey: statsKey(project, range),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs/stats", { params: { path: { project }, query: { days: range } }, signal })),
    placeholderData: keepPreviousData,
  });
