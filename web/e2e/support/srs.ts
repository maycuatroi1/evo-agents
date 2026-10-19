import { readFileSync } from "node:fs";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account } from "./hub";
import { apiOf } from "./memories";
import { enablePlugin } from "./plugins";

/**
 * The core plugin srs through the real API, as `evo-agents hub srs import|submit|approve` does: the sample SRS of the
 * Python tests (tests/hub/fixtures/srs/demo-shop-srs.md, no client's data), imported, edited, submitted and reviewed
 * with a member's machine token, which needs no CSRF header.
 */
export type SrsVersion = components["schemas"]["SrsVersion"];
export type SrsChange = components["schemas"]["SrsChange"];

export const SAMPLE_SRS = readFileSync(new URL("../../../tests/hub/fixtures/srs/demo-shop-srs.md", import.meta.url), "utf8");
export const SAMPLE_SLUG = "demo-shop";
export const SAMPLE_TITLE = "Demo Shop: Order Service";
/** The sample's use case UC ORD-1, its first business rule, and a screen. */
export const USE_CASE = { code: "UC ORD-1", title: "Place an order" };
export const RULE = { code: "BR ORD1-1", title: "Cart rules" };

export function srsPath(project: string, rest = ""): string {
  return `/p/${project}/x/srs/documents${rest}`;
}

export function documentPath(project: string, rest = "", slug = SAMPLE_SLUG): string {
  return srsPath(project, `/${slug}${rest}`);
}

export function itemPath(project: string, code: string, slug = SAMPLE_SLUG): string {
  return documentPath(project, `/items/${encodeURIComponent(code)}`, slug);
}

/** Enable srs in `project` as `account`, an admin of it; it asks for no permission. */
export async function enableSrs(account: Account, project: string) {
  return enablePlugin(account, project, "srs", []);
}

/** Enable tasks in `project` as `account`, granting what it asks for (it reads the project's plans). */
export async function enableTasks(account: Account, project: string) {
  return enablePlugin(account, project, "tasks", ["plans:read"]);
}

/** Import the sample as a new document (`slug`) as `account`, a writer; the version saved, v1, a draft. */
export async function importSample(account: Account, project: string, slug = SAMPLE_SLUG): Promise<number> {
  const api = await apiOf(account);
  const done = await call(
    api.POST("/v1/x/srs/{project}/import", {
      params: { path: { project } },
      body: { markdown: SAMPLE_SRS, slug, kind: "srs", summary: "Imported", dry_run: false },
    }),
  );
  return done.version ?? 1;
}

/** The latest version of a document whole, as `account` reads it. */
export async function latestOf(account: Account, project: string, slug = SAMPLE_SLUG) {
  const api = await apiOf(account);
  return call(api.GET("/v1/x/srs/{project}/documents/{slug}", { params: { path: { project, slug } } }));
}

/** Change the body of the item with `code` in the latest version as `account`: the next version, a draft. */
export async function editItem(account: Account, project: string, code: string, body: (held: string) => string, slug = SAMPLE_SLUG) {
  const doc = await latestOf(account, project, slug);
  const item = doc.shown?.items.find((one) => one.code === code);
  if (!item || !doc.latest_version) throw new Error(`no item ${code} in ${slug}`);
  const api = await apiOf(account);
  const change: SrsChange = { key: item.key, body: body(item.body), retire: false };
  const saved = await call(
    api.POST("/v1/x/srs/{project}/documents/{slug}/versions", {
      params: { path: { project, slug } },
      body: { base_version: doc.latest_version, summary: `Edit ${code}`, changes: [change] },
    }),
  );
  return saved.number;
}

export async function submitVersion(account: Account, project: string, number: number, slug = SAMPLE_SLUG): Promise<SrsVersion> {
  const api = await apiOf(account);
  return call(api.POST("/v1/x/srs/{project}/documents/{slug}/versions/{number}/submit", { params: { path: { project, slug, number } } }));
}

export async function reviewVersion(
  account: Account,
  project: string,
  number: number,
  decision: "approve" | "request_changes",
  body = "",
  slug = SAMPLE_SLUG,
): Promise<SrsVersion> {
  const api = await apiOf(account);
  return call(
    api.POST("/v1/x/srs/{project}/documents/{slug}/versions/{number}/reviews", {
      params: { path: { project, slug, number } },
      body: { decision, role: null, body },
    }),
  );
}
