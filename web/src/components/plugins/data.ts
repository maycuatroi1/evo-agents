"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { browserApi } from "@/lib/api/browser";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { type PluginConfigLayer, type PluginConfigWrite, pluginKeys } from "@/lib/plugin-queries";
import { whoamiQuery } from "@/lib/queries";

/**
 * The writes of the plugin pages, each with the session's X-Evo-CSRF header: the store's install, upgrade and remove
 * (hub admins), a project's enable (naming back exactly what the plugin asks for, the consent) and disable (admins of
 * the project), and the writes and rollbacks of a config layer, the hub's own (`project` null) or a project's. Each
 * reloads what it changed whatever the hub answered, so the pages show what the hub holds; a 401 sends the visitor to
 * sign in.
 */

export type StoreAction = "install" | "upgrade" | "remove";
export type LayerTarget = { plugin: string; project: string | null };

async function storeAction(api: ApiClient, name: string, action: StoreAction, version: string | null) {
  const headers = await csrfHeaders(api);
  const params = { path: { name } };
  if (action === "remove") return call(api.POST("/v1/plugins/{name}/remove", { params, headers }));
  const body = { version };
  return action === "install"
    ? call(api.POST("/v1/plugins/{name}/install", { params, body, headers }))
    : call(api.POST("/v1/plugins/{name}/upgrade", { params, body, headers }));
}

async function enablePlugin(api: ApiClient, project: string, name: string, permissions: string[]) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, name } };
  return call(api.POST("/v1/projects/{project}/plugins/{name}/enable", { params, body: { permissions }, headers }));
}

async function disablePlugin(api: ApiClient, project: string, name: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/plugins/{name}/disable", { params: { path: { project, name } }, headers }));
}

async function writeLayer(api: ApiClient, { plugin, project }: LayerTarget, body: PluginConfigWrite): Promise<PluginConfigLayer> {
  const headers = await csrfHeaders(api);
  if (project === null) {
    return call(api.POST("/v1/plugins/{name}/config", { params: { path: { name: plugin } }, body, headers }));
  }
  const params = { path: { project, name: plugin } };
  return call(api.POST("/v1/projects/{project}/plugins/{name}/config", { params, body, headers }));
}

async function rollbackLayer(
  api: ApiClient,
  { plugin, project }: LayerTarget,
  body: { version: number; if_version: number },
): Promise<PluginConfigLayer> {
  const headers = await csrfHeaders(api);
  if (project === null) {
    return call(api.POST("/v1/plugins/{name}/config/rollback", { params: { path: { name: plugin } }, body, headers }));
  }
  const params = { path: { project, name: plugin } };
  return call(api.POST("/v1/projects/{project}/plugins/{name}/config/rollback", { params, body, headers }));
}

function signInAgainOn401(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/** What a write of `plugin` may have changed: the store's view of it and every project's. */
function useRefresh() {
  const queryClient = useQueryClient();
  return useCallback(
    (project: string | null) => {
      void queryClient.invalidateQueries({ queryKey: pluginKeys.store });
      void queryClient.invalidateQueries({ queryKey: project ? pluginKeys.project(project) : ["projects"] });
    },
    [queryClient],
  );
}

export function useStoreAction() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ name, action, version = null }: { name: string; action: StoreAction; version?: string | null }) =>
      storeAction(browserApi(), name, action, version),
    onError: signInAgainOn401,
    onSettled: () => refresh(null),
  });
}

export function useEnablePlugin(project: string) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ name, permissions }: { name: string; permissions: string[] }) =>
      enablePlugin(browserApi(), project, name, permissions),
    onError: signInAgainOn401,
    onSettled: () => refresh(project),
  });
}

export function useDisablePlugin(project: string) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (name: string) => disablePlugin(browserApi(), project, name),
    onError: signInAgainOn401,
    onSettled: () => refresh(project),
  });
}

export function useLayerWrite(target: LayerTarget) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (body: PluginConfigWrite) => writeLayer(browserApi(), target, body),
    onError: signInAgainOn401,
    onSettled: () => refresh(target.project),
  });
}

export function useLayerRollback(target: LayerTarget) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (body: { version: number; if_version: number }) => rollbackLayer(browserApi(), target, body),
    onError: signInAgainOn401,
    onSettled: () => refresh(target.project),
  });
}

type Overrides = Partial<Record<403 | 404 | 409 | 422, string>>;

/** What to say when a write of a plugin fails: the shared words, the plugins' own for 403 and 409. */
export function usePluginFailure(): (error: unknown, overrides?: Overrides) => WriteFailure {
  const failure = useWriteFailure();
  const t = useTranslations("plugins.errors");
  return useCallback(
    (error: unknown, overrides: Overrides = {}) => {
      const found = failure(error, { 403: t("forbidden"), 409: t("conflict"), ...overrides });
      // The hub's message names what it refused (a field, a version, a permission): keep it beside ours.
      if (isApiError(error) && found.detail === null && error.info.message) {
        return { ...found, detail: error.info.message };
      }
      return found;
    },
    [failure, t],
  );
}

/** Whether the visitor is an admin of `project`: only an admin enables, disables and configures its plugins. */
export function useProjectAdmin(project: string): boolean {
  const { data } = useQuery(whoamiQuery(browserApi));
  return data?.grants.some((grant) => grant.project === project && grant.role === "admin") ?? false;
}
