import { createHash } from "node:crypto";

import { type ApiClient, call } from "../../src/lib/api/client";

import { STACK_URL } from "./env";

/**
 * Skills published through the real API, as `evo-agents hub skills publish` does: the bundle (packed by the
 * stack, which has the hub's packer) asked for, PUT to the presigned URL of the stack's moto S3, committed, then
 * published as the skill's next version.
 */
export type Bundle = { data: Buffer; sha256: string; size: number };

export async function packSkill(name: string, text: string, description?: string): Promise<Bundle> {
  const response = await fetch(`${STACK_URL}/skills/bundle`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, text, description, files: { "notes/usage.md": `# ${name}\n\n${text}\n` } }),
  });
  if (!response.ok) throw new Error(`hub_stack /skills/bundle: ${response.status} ${await response.text()}`);
  const packed = (await response.json()) as { data: string; sha256: string; size: number };
  const data = Buffer.from(packed.data, "base64");
  if (sha256(data) !== packed.sha256 || data.length !== packed.size) {
    throw new Error("the stack packed a bundle that does not hash to what it said");
  }
  return { data, sha256: packed.sha256, size: packed.size };
}

export function sha256(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

export type Source = { repo: string; commit: string };

export async function publishSkill(
  api: ApiClient,
  bundle: Bundle,
  name: string,
  project: string | null,
  source?: Source,
): Promise<number> {
  const holder = project ? { project } : {};
  const asked = await call(
    api.POST("/v1/blobs/uploads", {
      body: { ...holder, items: [{ sha256: bundle.sha256, size: bundle.size, kind: "skill-bundle" }] },
    }),
  );
  for (const ticket of asked.uploads) {
    const put = await fetch(ticket.url, {
      method: "PUT",
      body: new Uint8Array(bundle.data),
      headers: { "content-type": "application/octet-stream" },
    });
    if (!put.ok) throw new Error(`PUT to the presigned URL: ${put.status}`);
    await call(api.POST("/v1/blobs/commit", { body: { ...holder, upload_ids: [ticket.upload_id] } }));
  }
  const body = {
    sha256: bundle.sha256,
    size: bundle.size,
    ...(source ? { source_repo: source.repo, source_commit: source.commit } : {}),
  };
  const published = project
    ? await call(api.POST("/v1/skills/projects/{project}/{name}/versions", { params: { path: { project, name } }, body }))
    : await call(api.POST("/v1/skills/global/{name}/versions", { params: { path: { name } }, body }));
  return published.latest.version;
}
