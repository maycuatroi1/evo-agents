import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import type { Agent, AgentCreate, AgentEdit } from "@/lib/agents";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of a project's agent profiles (GET, POST /v1/projects/{p}/agents and below). Every member of
 * the project reads them, deleted ones included, since a run keeps the agent it went under; only an admin of the
 * project creates, changes and deletes one (a writer or reader gets 403). The Agents pages and the dispatch dialogs
 * share the list, so it is read once with the deleted agents and each caller filters it.
 */
export const agentKeys = {
  all: (project: string) => ["projects", project, "agents"] as const,
  list: (project: string) => ["projects", project, "agents", "list"] as const,
  one: (project: string, handle: string) => ["projects", project, "agents", "one", handle] as const,
  versions: (project: string, handle: string) => ["projects", project, "agents", "one", handle, "versions"] as const,
};

/** The project's agents, deleted ones included: the default first, then by handle. */
export const agentsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: agentKeys.list(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/agents", { params: { path: { project }, query: { deleted: true } }, signal })),
    staleTime: 30_000,
  });

export const agentQuery = (api: ApiSource, project: string, handle: string) =>
  queryOptions({
    queryKey: agentKeys.one(project, handle),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/agents/{handle}", { params: { path: { project, handle } }, signal })),
  });

/** Every version of the agent, the latest first. */
export const agentVersionsQuery = (api: ApiSource, project: string, handle: string) =>
  queryOptions({
    queryKey: agentKeys.versions(project, handle),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/agents/{handle}/versions", { params: { path: { project, handle } }, signal })),
  });

export function agentsHref(project: string): Route {
  return `/p/${encodeURIComponent(project)}/agents` as Route;
}

export function agentHref(project: string, handle: string): Route {
  return `${agentsHref(project)}/${encodeURIComponent(handle)}` as Route;
}

// Writes, each with the session's X-Evo-CSRF header.

export async function createAgent(api: ApiClient, project: string, body: AgentCreate): Promise<Agent> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/agents", { params: { path: { project } }, body, headers }));
}

/** Change the fields `body` names, as the agent's next version; 409 unless it is at `body.if_version` now. */
export async function editAgent(api: ApiClient, project: string, handle: string, body: AgentEdit): Promise<Agent> {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/projects/{project}/agents/{handle}", { params: { path: { project, handle } }, body, headers }));
}

/** Soft-delete: the agent takes no new run, and reads as before. */
export async function deleteAgent(api: ApiClient, project: string, handle: string): Promise<Agent> {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/projects/{project}/agents/{handle}", { params: { path: { project, handle } }, headers }));
}

/** Set the avatar to an image given as base64 (PNG, JPEG or WebP), as the agent's next version. */
export async function putAvatar(api: ApiClient, project: string, handle: string, data: string): Promise<Agent> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/agents/{handle}/avatar", { params: { path: { project, handle } }, body: { data }, headers }));
}

/** Drop the avatar, as the agent's next version: the web draws its initial on its colour again. */
export async function deleteAvatar(api: ApiClient, project: string, handle: string): Promise<Agent> {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/projects/{project}/agents/{handle}/avatar", { params: { path: { project, handle } }, headers }));
}
