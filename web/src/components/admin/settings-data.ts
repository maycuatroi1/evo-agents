import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The hub's own settings as hub admins change them (evo_agents.hub.server.hub_settings): the branding and the layers
 * of features, the hub's and each project's. Each is a layer of fields with versions; a write sets and unsets fields
 * and makes the next version, a rollback copies an older one, and `if_version` turns a change made meanwhile into a
 * 409 rather than writing over it.
 */
type Schemas = components["schemas"];
export type SettingsLayer = Schemas["SettingsLayer"];
export type SettingsWrite = Schemas["SettingsWrite"];
export type SettingsRollback = Schemas["SettingsRollback"];
export type ImageAsset = "logo" | "favicon";

export const hubSettingsKeys = {
  branding: ["admin", "settings", "branding"] as const,
  brandingVersions: ["admin", "settings", "branding", "versions"] as const,
  features: (project: string | null) => ["admin", "settings", "features", project ?? ""] as const,
  featureVersions: (project: string | null) => ["admin", "settings", "features", project ?? "", "versions"] as const,
};

export const brandingLayerQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: hubSettingsKeys.branding,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/branding", { signal })),
  });

export const brandingVersionsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: hubSettingsKeys.brandingVersions,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/branding/versions", { signal })),
  });

/** The hub's own layer of features (`project` null) or a project's. */
export const featureLayerQuery = (api: ApiSource, project: string | null) =>
  queryOptions({
    queryKey: hubSettingsKeys.features(project),
    queryFn: ({ signal }) =>
      call(
        project === null
          ? api().GET("/v1/admin/features", { signal })
          : api().GET("/v1/admin/projects/{project}/features", { params: { path: { project } }, signal }),
      ),
  });

export const featureVersionsQuery = (api: ApiSource, project: string | null) =>
  queryOptions({
    queryKey: hubSettingsKeys.featureVersions(project),
    queryFn: ({ signal }) =>
      call(
        project === null
          ? api().GET("/v1/admin/features/versions", { signal })
          : api().GET("/v1/admin/projects/{project}/features/versions", { params: { path: { project } }, signal }),
      ),
  });

export async function writeBranding(api: ApiClient, body: SettingsWrite): Promise<SettingsLayer> {
  return call(api.POST("/v1/admin/branding", { body, headers: await csrfHeaders(api) }));
}

export async function rollbackBranding(api: ApiClient, body: SettingsRollback): Promise<SettingsLayer> {
  return call(api.POST("/v1/admin/branding/rollback", { body, headers: await csrfHeaders(api) }));
}

export async function putBrandingImage(
  api: ApiClient,
  { asset, data, ifVersion }: { asset: ImageAsset; data: string; ifVersion: number },
): Promise<SettingsLayer> {
  const headers = await csrfHeaders(api);
  const body = { data, if_version: ifVersion };
  return call(api.PUT("/v1/admin/branding/{asset}", { params: { path: { asset } }, body, headers }));
}

export async function writeFeatures(
  api: ApiClient,
  { project, body }: { project: string | null; body: SettingsWrite },
): Promise<SettingsLayer> {
  const headers = await csrfHeaders(api);
  return call(
    project === null
      ? api.POST("/v1/admin/features", { body, headers })
      : api.POST("/v1/admin/projects/{project}/features", { params: { path: { project } }, body, headers }),
  );
}

export async function rollbackFeatures(
  api: ApiClient,
  { project, body }: { project: string | null; body: SettingsRollback },
): Promise<SettingsLayer> {
  const headers = await csrfHeaders(api);
  return call(
    project === null
      ? api.POST("/v1/admin/features/rollback", { body, headers })
      : api.POST("/v1/admin/projects/{project}/features/rollback", { params: { path: { project } }, body, headers }),
  );
}
