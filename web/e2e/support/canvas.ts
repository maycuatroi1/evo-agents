import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { STACK_URL } from "./env";
import { type Account } from "./hub";
import { apiOf } from "./memories";
import { enablePlugin } from "./plugins";

/**
 * The core plugin canvas and the project's signals through the real API, as `evo-agents hub canvas create|edit` and
 * `hub signal keys add|push` do, with a member's machine token, which needs no CSRF header; the value check through the
 * stack (POST /canvas/value-check), as the hub's worker runs it; and where the pages are. Names are fictional.
 */
export type CanvasChange = components["schemas"]["CanvasChangeIn"];
export type CanvasClaimDetail = components["schemas"]["CanvasClaimDetail"];
export type SignalIn = components["schemas"]["SignalIn"];

export const CANVAS_SLUG = "northwind";
export const CANVAS_NAME = "Northwind Checkout";
export const METRIC = "checkout.conversion";
export const TENANT = "lumen-shops";

/** A test card the value check reads: the mean conversion of the last 30 days at least 0.4, due in the future. */
export const CARD = {
  hypothesis: "Shop owners pay for one-click checkout",
  test: "Offer it to 20 shops for a month",
  metric: METRIC,
  aggregate: "mean",
  window_days: 30,
  comparator: ">=",
  threshold: 0.4,
  deadline: "2099-12-31",
} as const satisfies components["schemas"]["CanvasTestCardIn"];

export function canvasesPath(project: string): string {
  return `/p/${project}/x/canvas/canvases`;
}

export function canvasPath(project: string, slug = CANVAS_SLUG): string {
  return `${canvasesPath(project)}/${slug}`;
}

export function claimPath(project: string, code: string, slug = CANVAS_SLUG): string {
  return `${canvasPath(project, slug)}/claims/${code}`;
}

export function baselinePath(project: string, number: number, slug = CANVAS_SLUG): string {
  return `${canvasPath(project, slug)}/baselines/${number}`;
}

export function signalsPath(project: string, query = ""): string {
  return `/p/${project}/signals${query}`;
}

/** Enable canvas in `project` as `account`, an admin of it; it asks for no permission. */
export async function enableCanvas(account: Account, project: string) {
  return enablePlugin(account, project, "canvas", []);
}

export async function createCanvasByApi(account: Account, project: string, slug = CANVAS_SLUG, name = CANVAS_NAME) {
  const api = await apiOf(account);
  return call(api.POST("/v1/x/canvas/{project}/canvases", { params: { path: { project } }, body: { slug, name, description: "" } }));
}

/** Apply `changes` as `account`, a writer; the codes of the claims it wrote a rev of. */
export async function editByApi(account: Account, project: string, changes: CanvasChange[], reason = "", slug = CANVAS_SLUG): Promise<string[]> {
  const api = await apiOf(account);
  const done = await call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/edit", { params: { path: { project, slug } }, body: { changes, reason } }));
  return done.written;
}

export async function claimByApi(account: Account, project: string, code: string, slug = CANVAS_SLUG): Promise<CanvasClaimDetail> {
  const api = await apiOf(account);
  return call(api.GET("/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}", { params: { path: { project, slug, ref: code } } }));
}

/** Open a baseline of one claim at once as `account`, who then may not decide it; its number. */
export async function openBaselineByApi(account: Account, project: string, claim: string, slug = CANVAS_SLUG): Promise<number> {
  const api = await apiOf(account);
  const opened = await call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/baselines", { params: { path: { project, slug } }, body: { claim } }));
  return opened.number;
}

/** Add key `name` to the project's registry as `account`, an admin of it. */
export async function addKeyByApi(account: Account, project: string, name = METRIC, kind: "number" | "feedback" = "number", unit = "ratio") {
  const api = await apiOf(account);
  return call(
    api.POST("/v1/projects/{project}/signal-keys", {
      params: { path: { project } },
      body: { name, kind, unit, description: "Orders placed over carts opened", source: "lumen-analytics" },
    }),
  );
}

/** Push a batch of signals as `account`, a writer of the project. */
export async function pushByApi(account: Account, project: string, items: SignalIn[]) {
  const api = await apiOf(account);
  return call(api.POST("/v1/signals/{project}", { params: { path: { project } }, body: { items } }));
}

/** One pass of the value check over the project's canvases, as the hub's worker runs it on a batch of signals. */
export async function valueCheck(project: string): Promise<Record<string, number>> {
  const response = await fetch(`${STACK_URL}/canvas/value-check`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project }),
  });
  if (!response.ok) throw new Error(`hub_stack /canvas/value-check: ${response.status} ${await response.text()}`);
  return (await response.json()) as Record<string, number>;
}

/**
 * A canvas of `project` as `owner`, an admin of it: canvas enabled, the key checkout.conversion in the registry, and
 * three claims: CL-1 in Problem in testing with a test card on that key; CL-2 in Channels, an assumption whose test
 * card passed its deadline without evidence; CL-3 in the value proposition, an assumption.
 */
export async function seedCanvas(owner: Account, project: string) {
  await enableCanvas(owner, project);
  await addKeyByApi(owner, project);
  await createCanvasByApi(owner, project);
  await editByApi(owner, project, [
    { cell: "problem", text: "Shop owners lose a sale when checkout takes more than two minutes", status: "testing", confidence: "medium", test_card: CARD, signal_keys: [METRIC] },
    { cell: "channels", text: "Shops find us through their accountants", test_card: { hypothesis: "Accountants refer shops", test: "Ask ten accountants", deadline: "2020-01-31" } },
    { cell: "unique_value_proposition", text: "Checkout in one tap, on any shop" },
  ]);
}

/** Three conversions for the tenant lumen-shops whose mean meets CL-1's test card, measured over the last days. */
export async function pushConversions(account: Account, project: string) {
  const day = 24 * 3600 * 1000;
  return pushByApi(
    account,
    project,
    [0.42, 0.45, 0.5].map((value, index) => ({ key: METRIC, value, tenant: TENANT, synthetic: false, measured_at: new Date(Date.now() - (3 - index) * day).toISOString() })),
  );
}
