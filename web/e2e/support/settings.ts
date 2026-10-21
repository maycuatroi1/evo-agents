import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { ADMIN_ACCOUNT, bearerClient, machineToken } from "./hub";

/**
 * The hub's own settings through the real API as its hub admin (evo_agents.hub.server.branding and .features): the
 * branding and the layers of features, the hub's and a project's, each written at the version it holds now.
 */
type SettingsWrite = components["schemas"]["SettingsWrite"];

let admin: Promise<ApiClient> | null = null;

function api(): Promise<ApiClient> {
  admin ??= machineToken(ADMIN_ACCOUNT).then(bearerClient);
  return admin;
}

/** Write the hub's own layer of features (`project` null) or a project's: what `body` sets and unsets. */
export async function setFeatures(project: string | null, body: Omit<SettingsWrite, "if_version">) {
  const client = await api();
  const write = { values: {}, unset: [], ...body };
  return project === null
    ? call(client.POST("/v1/admin/features", { body: write }))
    : call(client.POST("/v1/admin/projects/{project}/features", { params: { path: { project } }, body: write }));
}

/** Unset every field of the hub's own layer of features and of the branding: the hub as it was with nothing set. */
export async function clearHubSettings(): Promise<void> {
  const client = await api();
  const features = await call(client.GET("/v1/admin/features"));
  if (Object.keys(features.values).length) await setFeatures(null, { unset: Object.keys(features.values) });
  const branding = await call(client.GET("/v1/admin/branding"));
  const fields = Object.keys(branding.values);
  if (fields.length) await call(client.POST("/v1/admin/branding", { body: { values: {}, unset: fields } }));
}

/** A small PNG: a 1 by 1 pixel image, valid enough for a browser to draw. */
export const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);
