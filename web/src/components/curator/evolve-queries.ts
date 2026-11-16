import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { CURATOR_IDLE_MS, curatorKeys } from "./queries";

/**
 * The Curator's evolutions (docs/curator.md, Evolving a harness): each run that rewrites a harness's files in a repo,
 * or an agent's configuration, over a few iterations, with the candidates on its Pareto front. Any member with a grant
 * reads them; the tasks' verify commands never leave the hub.
 */
type Schemas = components["schemas"];
export type Evolution = Schemas["EvolveView"];
const EVOLUTION_STATES = ["queued", "running", "done", "failed"] as const;
export type EvolutionState = (typeof EVOLUTION_STATES)[number];

export function isEvolutionState(state: string): state is EvolutionState {
  return (EVOLUTION_STATES as readonly string[]).includes(state);
}

/** The evolutions the card lists: the newest. */
export const EVOLUTIONS_LISTED = 10;

export const evolveKeys = {
  list: (project: string) => [...curatorKeys.all(project), "evolve"] as const,
};

export const evolutionsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: evolveKeys.list(project),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/evolve", {
          params: { path: { project }, query: { limit: EVOLUTIONS_LISTED } },
          signal,
        }),
      ),
    refetchInterval: CURATOR_IDLE_MS,
  });

/** What an evolution rewrites, as one line: the repo and its paths, or the agent of an agent system. */
export function evolutionSubject(item: Evolution): string {
  if (item.target === "agent_config") return item.agent ?? "";
  const paths = item.paths.length > 0 ? `: ${item.paths.join(", ")}` : "";
  return `${item.repo ?? ""}${paths}`;
}
