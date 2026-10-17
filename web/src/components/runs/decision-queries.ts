import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

type Schemas = components["schemas"];

// The decisions a plan run's agent asks its owner (docs/notifications.md); the inbox answers them.

export type Decision = Schemas["Decision"];
export type DecisionList = Schemas["DecisionList"];

/** Where the web shows a decision and its answer form, as the hub's notifications link to it (`decision_link`). */
export function decisionHref(id: number): Route {
  return `/inbox?decision=${id}` as Route;
}

/** The id of a decision's card on its run's page, which the trace's "Asked you" links to. */
export function decisionAnchor(id: number): string {
  return `run-decision-${id}`;
}

/** `MAX_OPEN_DECISIONS` of the API: a run's agent has at most 20 decisions open at once. */
export const MAX_OPEN_DECISIONS = 20;

/**
 * The open decisions of one run, newest first, all of them: the plan's banner links to the latest, and the run's page
 * shows each with its answer form.
 */
export const openDecisionsQuery = (api: ApiSource, project: string, runId: number) =>
  queryOptions({
    queryKey: ["projects", project, "decisions", { run: runId, state: "open" }] as const,
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/decisions", {
          params: { path: { project }, query: { state: ["open"], run_id: runId, limit: MAX_OPEN_DECISIONS } },
          signal,
        }),
      ),
  });
