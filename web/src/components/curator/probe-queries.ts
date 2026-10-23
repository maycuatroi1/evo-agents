import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { CURATOR_IDLE_MS, curatorKeys } from "./queries";

/**
 * What the Curator's probes read (docs/curator.md): the weekly report of the project's probes (each probe of a merged
 * change, on each runtime and model the charter names, with the rule on and the rule off), and the probes themselves,
 * whose task any member reads and whose verify the API hands to the project's admins alone. Any member with a grant
 * reads both; what each probe's status means is the hub's to say, never the page's.
 */
type Schemas = components["schemas"];
export type Probe = Schemas["Probe"];
export type ProbeWeekly = Schemas["ProbeWeekly"];
export type ProbeWeek = Schemas["ProbeWeek"];
export type ProbeWeekResult = Schemas["ProbeWeekResult"];
export type ProbeArmCount = Schemas["ProbeArmCount"];
export type ProbeFix = Schemas["ProbeFix"];
export type ProbeUnprobed = Schemas["ProbeUnprobed"];
export type ProbeStatus = ProbeWeekResult["status"];

/** The weeks the Curator's page and a proposal's probe card read: the week in progress and the seven before it. */
export const PROBE_WEEKS = 8;
/** The probes the report's rows are named from: the API's most. */
export const PROBES_LISTED = 200;

export const probeKeys = {
  report: (project: string, weeks: number) => [...curatorKeys.all(project), "probe-report", weeks] as const,
  list: (project: string, change: number | null) => [...curatorKeys.all(project), "probes", change] as const,
};

export const probeReportQuery = (api: ApiSource, project: string, weeks = PROBE_WEEKS) =>
  queryOptions({
    queryKey: probeKeys.report(project, weeks),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/curator/probe-report", { params: { path: { project }, query: { weeks } }, signal })),
    refetchInterval: CURATOR_IDLE_MS,
  });

/** The project's probes, newest first, retired ones too (a past week names them); those of change `change` alone when given. */
export const probesQuery = (api: ApiSource, project: string, change: number | null = null) =>
  queryOptions({
    queryKey: probeKeys.list(project, change),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/probes", {
          params: { path: { project }, query: { limit: PROBES_LISTED, ...(change === null ? {} : { change }) } },
          signal,
        }),
      ),
    refetchInterval: CURATOR_IDLE_MS,
  });
