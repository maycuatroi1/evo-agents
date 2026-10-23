import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Browser identities page reads and writes (docs/browser.md): the identities of a project the visitor may use
 * (GET .../browser-identities), a login session for one (POST .../login, a run of kind browser), and its switches. The
 * API decides again on every write.
 */
type Schemas = components["schemas"];
export type BrowserIdentity = Schemas["BrowserIdentity"];
export type BrowserIdentityCreate = Schemas["BrowserIdentityCreate"];
export type Worker = Schemas["Worker"];

/** The page reads the list again this often, so a login that was saved shows its new version. */
export const IDENTITIES_REFRESH_MS = 15_000;
/** `browser_login.MAX_URL_CHARS` of the hub. */
export const MAX_URL_CHARS = 2000;
export const BROWSER_SEGMENT = "browser";

export const identityKeys = {
  all: (project: string) => ["projects", project, "browser-identities"] as const,
};

export function browserHref(project: string): Route {
  return projectHref(project, BROWSER_SEGMENT);
}

export function runHref(project: string, id: number): Route {
  return projectHref(project, `runs/${id}`);
}

export const identitiesQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: identityKeys.all(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/browser-identities", { params: { path: { project } }, signal })),
    refetchInterval: IDENTITIES_REFRESH_MS,
  });

export const workersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: ["workers", "list", "browser-identities"] as const,
    queryFn: ({ signal }) => call(api().GET("/v1/workers", { params: { query: { revoked: false } }, signal })),
  });

// Writes, each with the session's X-Evo-CSRF header.

export async function createIdentity(api: ApiClient, project: string, body: BrowserIdentityCreate): Promise<BrowserIdentity> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/browser-identities", { params: { path: { project } }, headers, body }));
}

/** A login session; `worker` set moves the identity to that worker once the session saves. */
export async function loginIdentity(api: ApiClient, project: string, id: number, worker: string | null): Promise<Schemas["Run"]> {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/browser-identities/{identity_id}/login", {
      params: { path: { project, identity_id: id } },
      headers,
      body: { worker },
    }),
  );
}

export async function disableIdentity(api: ApiClient, project: string, id: number): Promise<BrowserIdentity> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/browser-identities/{identity_id}/disable", { params: { path: { project, identity_id: id } }, headers }));
}

export async function enableIdentity(api: ApiClient, project: string, id: number): Promise<BrowserIdentity> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/browser-identities/{identity_id}/enable", { params: { path: { project, identity_id: id } }, headers }));
}

export async function deleteIdentity(api: ApiClient, project: string, id: number): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/projects/{project}/browser-identities/{identity_id}", { params: { path: { project, identity_id: id } }, headers }));
}
