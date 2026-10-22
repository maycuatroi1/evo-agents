import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pluginPageHref } from "@/lib/plugin-queries";
import type { ApiSource } from "@/lib/queries";

/**
 * The read of the core plugin pm (evo_agents.core_plugins.pm.server, GET /v1/x/pm/{project}/daily), as TanStack Query
 * options the server (prefetch) and the browser share. The report of one day is one query; `date` null asks for today
 * in the project's timezone, which the report then names.
 */
type Schemas = components["schemas"];
export type PmDaily = Schemas["PmDaily"];
export type PmBlocked = Schemas["PmBlocked"];

export const PM_PLUGIN = "pm";
/** The plugin's page in the project's menu. */
export const PM_DAILY = "daily";

export const pmKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", PM_PLUGIN] as const,
  daily: (project: string, date: string | null) => [...pmKeys.all(project), "daily", date ?? "today"] as const,
};

export const pmDailyQuery = (api: ApiSource, project: string, date: string | null) =>
  queryOptions({
    queryKey: pmKeys.daily(project, date),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/pm/{project}/daily", { params: { path: { project }, query: date ? { date } : {} }, signal })),
  });

/** A day as the route takes it, YYYY-MM-DD; null for anything else, so a hand-written URL asks for today. */
export function validDate(value: string | null | undefined): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  return Number.isNaN(Date.parse(`${value}T00:00:00Z`)) ? null : value;
}

export function dailyHref(project: string, date: string | null = null) {
  const base = pluginPageHref(project, PM_PLUGIN, PM_DAILY);
  return date ? (`${base}?date=${date}` as typeof base) : base;
}
