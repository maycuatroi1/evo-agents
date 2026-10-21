import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import type { components } from "@/lib/api/schema";
import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of plugins that the shell makes too (docs/plugins.md): a project's plugins, for its members (GET
 * /v1/projects/{p}/plugins and .../plugins/{name}: what each asks for, whether the project enabled it, its pages),
 * which the menu and the guard of a plugin's pages read, the keys of every plugin query, and where a plugin's pages
 * are. The reads only the plugin pages make (the store, the layers of a config) are components/plugins/queries.ts,
 * so the shell's chunks do not carry them.
 */
type Schemas = components["schemas"];
export type Plugin = Schemas["Plugin"];
export type PluginDetail = Schemas["PluginDetail"];
export type PluginVersion = Schemas["PluginVersion"];
export type PluginPublisher = Schemas["PluginPublisher"];
export type BackendStatus = Schemas["BackendStatus"];
export type ProjectPlugin = Schemas["ProjectPlugin"];
export type PluginPage = Schemas["PluginPage"];
export type PluginPermission = Schemas["PluginPermission"];
export type PluginConfigLayer = Schemas["PluginConfigLayer"];
export type PluginConfigWrite = Schemas["PluginConfigWrite"];
export type EffectiveConfig = Schemas["EffectiveConfig"];

export const pluginKeys = {
  store: ["plugins"] as const,
  storeList: ["plugins", "list"] as const,
  storeOne: (name: string) => ["plugins", "one", name] as const,
  backend: (name: string) => ["plugins", "one", name, "backend"] as const,
  instance: (name: string) => ["plugins", "one", name, "config"] as const,
  instanceVersions: (name: string) => ["plugins", "one", name, "config", "versions"] as const,
  publishers: ["plugin-publishers"] as const,
  project: (project: string) => ["projects", project, "plugins"] as const,
  projectList: (project: string) => ["projects", project, "plugins", "list"] as const,
  projectOne: (project: string, name: string) => ["projects", project, "plugins", "one", name] as const,
  projectConfig: (project: string, name: string) => ["projects", project, "plugins", "one", name, "config"] as const,
  projectVersions: (project: string, name: string) =>
    ["projects", project, "plugins", "one", name, "config", "versions"] as const,
  effective: (project: string, name: string) => ["projects", project, "plugins", "one", name, "effective"] as const,
};

// A project's plugins, for its members

export const projectPluginsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: pluginKeys.projectList(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/plugins", { params: { path: { project } }, signal })),
  });

export const projectPluginQuery = (api: ApiSource, project: string, name: string) =>
  queryOptions({
    queryKey: pluginKeys.projectOne(project, name),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/plugins/{name}", { params: { path: { project, name } }, signal })),
  });

// Where things are

export function projectPluginsHref(project: string): Route {
  return `/p/${encodeURIComponent(project)}/plugins` as Route;
}

export function projectPluginHref(project: string, name: string): Route {
  return `${projectPluginsHref(project)}/${encodeURIComponent(name)}` as Route;
}

/** A plugin's page in a project: /p/<project>/x/<plugin>/<page>, framed or native. */
export function pluginPageHref(project: string, plugin: string, page: string): Route {
  return `/p/${encodeURIComponent(project)}/x/${encodeURIComponent(plugin)}/${encodeURIComponent(page)}` as Route;
}

/** A plugin's id, as its manifest gives it (evo_agents.plugins.manifest.PLUGIN_ID). */
export const PLUGIN_NAME = /^[a-z][a-z0-9-]{0,62}$/;
/** A page's name (plugin.schema.json, $defs.page). */
export const PAGE_NAME = /^[a-z][a-z0-9_-]{0,62}$/;
