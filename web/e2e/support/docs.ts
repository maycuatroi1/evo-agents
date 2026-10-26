import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { PIXEL_PNG } from "./agents";
import { API_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";
import { enablePlugin } from "./plugins";

/**
 * The hub's feature docs through the real API, as `evo-agents hub docs put` does: an image uploaded as the body of a
 * request, then a document saved naming it, with a member's machine token, which needs no CSRF header.
 */
export type DocsSaved = components["schemas"]["DocsSaved"];

export const GUIDE_SLUG = "guide";
export const GUIDE_TITLE = "Coordination guide";
export const GUIDE_BODY = '## 1. The hub\n\nOpen the board first.\n\n![The board](shots/board.png "Apps > Tasks")\n';

export function docsPath(project: string, rest = ""): string {
  return `/p/${project}/x/docs${rest}`;
}

export function guidePath(project: string, rest = ""): string {
  return docsPath(project, `/documents/${GUIDE_SLUG}${rest}`);
}

export async function enableDocs(account: Account, project: string) {
  return enablePlugin(account, project, "docs", []);
}

/** Upload `bytes` as an image of the project's documents; its SHA-256. */
export async function uploadImageByApi(account: Account, project: string, bytes: Buffer = PIXEL_PNG): Promise<string> {
  const token = await machineToken(account);
  const response = await fetch(`${API_URL}/v1/x/docs/${project}/images`, {
    method: "POST",
    body: new Uint8Array(bytes),
    headers: { authorization: `Bearer ${token}`, "content-type": "image/png", accept: "application/json" },
  });
  if (!response.ok) throw new Error(`the image upload answered ${response.status}: ${await response.text()}`);
  return ((await response.json()) as { sha256: string }).sha256;
}

/** The guide saved as `account`, showing the pixel as shots/board.png. */
export async function seedGuide(account: Account, project: string, body = GUIDE_BODY): Promise<DocsSaved> {
  const sha256 = await uploadImageByApi(account, project);
  const api = bearerClient(await machineToken(account));
  return call(
    api.PUT("/v1/x/docs/{project}/documents/{slug}", {
      params: { path: { project, slug: GUIDE_SLUG } },
      body: { title: GUIDE_TITLE, body, summary: "First draft", images: [{ name: "shots/board.png", sha256 }] },
    }),
  );
}
