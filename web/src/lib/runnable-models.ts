import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";
import type { RequestedRuntime } from "@/lib/runtimes";

/**
 * The models of provider accounts the visitor may run in a project (GET /v1/projects/{project}/runnable-models,
 * docs/credentials.md, section "Provider accounts"): each named as a run asks for it, `<prefix>/<model>`, with the
 * runtimes that run it through the hub's model gateway and the accounts that serve it. The dispatch forms offer them
 * in their Model field and the Accounts page lists them in its Models tab; neither carries a key.
 */
export type RunnableModel = components["schemas"]["RunnableModel"];

export const runnableModelKeys = {
  all: ["runnable-models"] as const,
  project: (project: string) => ["runnable-models", project] as const,
};

export const runnableModelsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: runnableModelKeys.project(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/runnable-models", { params: { path: { project } }, signal })),
    staleTime: 30_000,
  });

/** The models a run on `runtime` may ask for: those a runtime of theirs runs, or every one for any. */
export function modelsFor(models: readonly RunnableModel[], runtime: RequestedRuntime): RunnableModel[] {
  return runtime === "any" ? [...models] : models.filter((model) => model.runtimes.includes(runtime));
}

/** The models by their prefix (the provider's alias), in the order the hub lists them, which is by name. */
export function byPrefix(models: readonly RunnableModel[]): { prefix: string; models: RunnableModel[] }[] {
  const groups = new Map<string, RunnableModel[]>();
  for (const model of models) groups.set(model.prefix, [...(groups.get(model.prefix) ?? []), model]);
  return [...groups].map(([prefix, listed]) => ({ prefix, models: listed }));
}
