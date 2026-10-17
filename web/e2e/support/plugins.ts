import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { STACK_URL } from "./env";
import { ADMIN_ACCOUNT, type Account, uniqueName } from "./hub";
import { apiOf } from "./memories";

/**
 * Plugins through the real API, as `evo-agents hub plugin publish|install` and `hub plugin enable` do: the stack packs
 * the sample plugin of tests/fixtures/plugins/sample under a name of the spec's and signs it with a key the hub
 * trusts (hub_stack.py, POST /plugins/bundle), the hub admin uploads, commits, publishes and installs it, and an admin
 * of a project enables it there. Each spec takes a plugin of its own, so specs running in parallel never share one.
 */
export type Plugin = components["schemas"]["Plugin"];
export type ProjectPlugin = components["schemas"]["ProjectPlugin"];

/** What the sample plugin asks for, and the page it adds (tests/fixtures/plugins/sample/evo-plugin.json). */
export const SAMPLE_PERMISSIONS = ["runs:read"];
export const SAMPLE_PAGE = { name: "board", title: "Sample board" };

type Packed = { data: Buffer; sha256: string; size: number; signature: Record<string, unknown> };

async function pack(id: string, version: string): Promise<Packed> {
  const response = await fetch(`${STACK_URL}/plugins/bundle`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ id, version }),
  });
  if (!response.ok) throw new Error(`hub_stack /plugins/bundle: ${response.status} ${await response.text()}`);
  const packed = (await response.json()) as { data: string; sha256: string; size: number; signature: Record<string, unknown> };
  return { ...packed, data: Buffer.from(packed.data, "base64") };
}

async function admin(): Promise<ApiClient> {
  return apiOf(ADMIN_ACCOUNT);
}

/** Publish version `version` of the sample plugin as plugin `id`, signed by acme; installs nothing. */
export async function publishSample(id: string, version = "1.0.0"): Promise<void> {
  const api = await admin();
  const bundle = await pack(id, version);
  const asked = await call(
    api.POST("/v1/blobs/uploads", { body: { items: [{ sha256: bundle.sha256, size: bundle.size, kind: "plugin-bundle" }] } }),
  );
  for (const ticket of asked.uploads) {
    const put = await fetch(ticket.url, {
      method: "PUT",
      body: new Uint8Array(bundle.data),
      headers: { "content-type": "application/octet-stream" },
    });
    if (!put.ok) throw new Error(`PUT to the presigned URL: ${put.status}`);
    await call(api.POST("/v1/blobs/commit", { body: { upload_ids: [ticket.upload_id] } }));
  }
  const body = { sha256: bundle.sha256, size: bundle.size, signature: bundle.signature };
  await call(api.POST("/v1/plugins/{name}/versions", { params: { path: { name: id } }, body }));
}

export async function installPlugin(id: string, version: string | null = null): Promise<Plugin> {
  const api = await admin();
  return call(api.POST("/v1/plugins/{name}/install", { params: { path: { name: id } }, body: { version } }));
}

/** A fresh sample plugin, published and installed; its id. */
export async function installedSample(prefix = "plugin"): Promise<string> {
  const id = uniqueName(prefix);
  await publishSample(id);
  await installPlugin(id);
  return id;
}

/** Enable plugin `id` in `project` as `account`, an admin of the project, granting what it asks for. */
export async function enablePlugin(account: Account, project: string, id: string, permissions = SAMPLE_PERMISSIONS): Promise<ProjectPlugin> {
  const api = await apiOf(account);
  return call(
    api.POST("/v1/projects/{project}/plugins/{name}/enable", { params: { path: { project, name: id } }, body: { permissions } }),
  );
}

export async function projectPluginOf(account: Account, project: string, id: string): Promise<ProjectPlugin> {
  const api = await apiOf(account);
  return call(api.GET("/v1/projects/{project}/plugins/{name}", { params: { path: { project, name: id } } }));
}

/** The project's layer of plugin `id`'s config, as the hub holds it. */
export async function projectConfigOf(account: Account, project: string, id: string) {
  const api = await apiOf(account);
  return call(api.GET("/v1/projects/{project}/plugins/{name}/config", { params: { path: { project, name: id } } }));
}

export function pluginPagePath(project: string, id: string, page = SAMPLE_PAGE.name): string {
  return `/p/${project}/x/${id}/${page}`;
}
