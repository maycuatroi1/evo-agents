import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { call } from "@/lib/api/client";
import type { ApiError } from "@/lib/api/errors";
import { type PluginConfigLayer, pluginKeys } from "@/lib/plugin-queries";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of the plugin pages that the shell never makes (lib/plugin-queries.ts has those): the hub's store, for
 * hub admins, and the layers of a plugin's config with their versions and the effective config of a project.
 */

// The store, for hub admins

export const pluginsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: pluginKeys.storeList,
    queryFn: ({ signal }) => call(api().GET("/v1/plugins", { signal })),
  });

export const pluginQuery = (api: ApiSource, name: string) =>
  queryOptions({
    queryKey: pluginKeys.storeOne(name),
    queryFn: ({ signal }) => call(api().GET("/v1/plugins/{name}", { params: { path: { name } }, signal })),
  });

export const publishersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: pluginKeys.publishers,
    queryFn: ({ signal }) => call(api().GET("/v1/plugin-publishers", { signal })),
    staleTime: 5 * 60_000, // the operator's environment: it changes with a restart of the hub
  });

/** The state of an isolated plugin's backend as the plugin host last reported it. */
export const backendQuery = (api: ApiSource, name: string) =>
  queryOptions({
    queryKey: pluginKeys.backend(name),
    queryFn: ({ signal }) => call(api().GET("/v1/plugins/{name}/backend", { params: { path: { name } }, signal })),
    refetchInterval: 15_000,
  });

export const instanceConfigQuery = (api: ApiSource, name: string) =>
  queryOptions({
    queryKey: pluginKeys.instance(name),
    queryFn: ({ signal }) => call(api().GET("/v1/plugins/{name}/config", { params: { path: { name } }, signal })),
  });

export const instanceVersionsQuery = (api: ApiSource, name: string) =>
  queryOptions({
    queryKey: pluginKeys.instanceVersions(name),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/plugins/{name}/config/versions", { params: { path: { name } }, signal })),
  });

export const projectConfigQuery = (api: ApiSource, project: string, name: string) =>
  queryOptions({
    queryKey: pluginKeys.projectConfig(project, name),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/plugins/{name}/config", { params: { path: { project, name } }, signal })),
  });

export const projectVersionsQuery = (api: ApiSource, project: string, name: string) =>
  queryOptions({
    queryKey: pluginKeys.projectVersions(project, name),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/plugins/{name}/config/versions", {
          params: { path: { project, name } },
          signal,
        }),
      ),
  });

/** What the plugin gets in the project for runs that name no agent: each field with the layer that set it. */
export const effectiveConfigQuery = (api: ApiSource, project: string, name: string) =>
  queryOptions({
    queryKey: pluginKeys.effective(project, name),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/plugins/{name}/config/effective", {
          params: { path: { project, name } },
          signal,
        }),
      ),
  });

/** The versions of one stored layer: the hub's own (`project` null) or a project's. */
export const layerVersionsQuery = (api: ApiSource, plugin: string, project: string | null) =>
  queryOptions<PluginConfigLayer[], ApiError, PluginConfigLayer[], readonly string[]>({
    queryKey: project === null ? pluginKeys.instanceVersions(plugin) : pluginKeys.projectVersions(project, plugin),
    queryFn: ({ signal }) =>
      call(
        project === null
          ? api().GET("/v1/plugins/{name}/config/versions", { params: { path: { name: plugin } }, signal })
          : api().GET("/v1/projects/{project}/plugins/{name}/config/versions", {
              params: { path: { project, name: plugin } },
              signal,
            }),
      ),
  });

export function adminPluginsHref(): Route {
  return "/admin/plugins";
}

export function adminPluginHref(name: string): Route {
  return `/admin/plugins/${encodeURIComponent(name)}` as Route;
}
