import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";

import type { NamedGraphUpdate } from "./types";

/**
 * The named graphs of a project (`evo_agents.hub.server.kg_graphs`, docs/kg.md, Named graphs): a name and a set of
 * the project's sources. The explorer's picker narrows its search to one with `?graph=`; `default` is every source
 * and is never in the URL. A writer creates, renames, gives or strips sources and deletes them here.
 */
export const DEFAULT_GRAPH = "default";
/** What a graph's name may be (`GRAPH_NAME_PATTERN` of `evo_agents.hub.tables_kg`). */
export const GRAPH_NAME = /^[a-z0-9][a-z0-9._-]{0,62}$/;
/** The longest description the hub keeps (`MAX_DESCRIPTION_CHARS` of `evo_agents.hub.kg_named_graphs`). */
export const MAX_GRAPH_DESCRIPTION = 500;

/** The named graph the URL names, or "" for `default`: a name that is not one reads every source. */
export function parseGraph(value: string | string[] | null | undefined): string {
  const text = (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
  return text !== DEFAULT_GRAPH && GRAPH_NAME.test(text) ? text : "";
}

/** Whether `name` can name a new graph. */
export function validGraphName(name: string): boolean {
  return name !== DEFAULT_GRAPH && GRAPH_NAME.test(name);
}

/** `sources` with `source` added when it is missing, taken out when it is there. */
export function toggled(sources: readonly string[], source: string): string[] {
  return sources.includes(source) ? sources.filter((s) => s !== source) : [...sources, source];
}

export async function createGraph(
  api: ApiClient,
  project: string,
  body: { name: string; sources: string[]; description: string },
) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/kg/{project}/graphs", { params: { path: { project } }, body, headers }));
}

export async function updateGraph(api: ApiClient, project: string, name: string, body: NamedGraphUpdate) {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/kg/{project}/graphs/{name}", { params: { path: { project, name } }, body, headers }));
}

export async function deleteGraph(api: ApiClient, project: string, name: string) {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/kg/{project}/graphs/{name}", { params: { path: { project, name } }, headers }));
}
