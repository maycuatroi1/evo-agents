import { type APIRequestContext, expect, type Page } from "@playwright/test";

import { STACK_URL } from "./env";
import { type Account, HubAdmin, LEVELS, newAccount, registration, type Role, uniqueName } from "./hub";

/**
 * A built knowledge graph for the specs: the synthetic fixture of `tests/hub/kg_fixture.py`, registered as a
 * project, pushed by a writer through the API and built by the worker's job inside the stack (`e2e/kg_seed.py`).
 * The ids below are that fixture's.
 */
export const AGENT = "claude-code@anthropic";
export const RUNBOOK = "docs:doc:runbook";
export const SHARED_NODE = "requirement:KB-01"; // mentioned by the runbook (internal) and the contract (customer)
export const HUB_NODE = "requirement:KB-77"; // 160 neighbours
export const CONTRACT = "deals:doc:acme-contract"; // customer
export const NOTES = 160;

export type Build = {
  id: number;
  status: "queued" | "running" | "succeeded" | "failed";
  content_hash: string | null;
  nodes: number | null;
  edges: number | null;
  error: string | null;
};

export type KgProject = { project: string; writer: Account; build: Build };

/** The registration `tests/hub/kg_fixture.py` pushes against: a hub sink that clears customer, two agent sinks. */
export function kgRegistration(project: string) {
  return registration({
    levels: LEVELS,
    locations: ["any"],
    default_label: { level: "internal", integrity: "U" },
    sinks: [
      { id: AGENT, kind: "agent-session", clearance: { level: "customer" } },
      { id: "narrow-agent", kind: "agent-session", clearance: { level: "internal" } },
      { id: "hub", kind: "hub", clearance: { level: "customer" } },
    ],
    repos: [],
    harness: { name: project, workspace: "~/ws", path: `${project}-harness` },
  });
}

async function stack<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** A new project with a writer, nothing pushed. */
export async function kgProject(admin: HubAdmin, prefix = "kg"): Promise<{ project: string; writer: Account }> {
  const project = uniqueName(prefix);
  const writer = newAccount("kg-writer");
  await admin.registerProject(project, kgRegistration(project));
  await admin.grant(project, writer.login, "writer", "customer");
  return { project, writer };
}

/** A new project with the fixture pushed and built; the build as the API returns it. */
export async function seededKg(admin: HubAdmin): Promise<KgProject> {
  const { project, writer } = await kgProject(admin);
  const build = await stack<Build>("/kg/seed", { project, login: writer.login, id: writer.id });
  expect(build.status, `the fixture's build: ${build.error ?? ""}`).toBe("succeeded");
  return { project, writer, build };
}

/** Queue a build of `project` as `writer`; with `run`, the stack runs it at once. */
export function queueBuild(project: string, writer: Account, run: boolean): Promise<Build> {
  return stack<Build>("/kg/build", { project, login: writer.login, id: writer.id, run });
}

let shared: Promise<KgProject> | null = null;

/** One seeded project per Playwright worker, for specs that only read it. */
export function sharedKg(): Promise<KgProject> {
  shared ??= seededKg(new HubAdmin());
  return shared;
}

export async function grantOn(project: string, login: string, role: Role, maxLevel: string): Promise<void> {
  await new HubAdmin().grant(project, login, role, maxLevel);
}

export function kgPath(project: string): string {
  return `/p/${project}/kg`;
}

export function nodePath(project: string, id: string, hops?: 1 | 2): string {
  const params = new URLSearchParams({ id });
  if (hops === 1) params.set("hops", "1");
  return `/p/${project}/kg/node?${params.toString()}`;
}

/** kg_context through the API's tool route, as an agent would read it, with the page's session. */
export async function kgContext(
  request: APIRequestContext,
  project: string,
  ids: string[],
  hops: number,
): Promise<{ nodes: { id: string }[]; edges: { src: string; rel: string; dst: string }[] }> {
  // A POST with the session cookie carries the session's CSRF header, as any write from the web would.
  const csrf = (await (await request.get("/v1/auth/web/csrf")).json()) as { csrf: string; header: string };
  const response = await request.post(`/v1/kg/${project}/tools/kg_context`, {
    data: { arguments: { ids, hops, budget_tokens: 8000 }, sink: AGENT },
    headers: { [csrf.header]: csrf.csrf },
  });
  expect(response.status()).toBe(200);
  const result = await response.json();
  expect(result.isError ?? false).toBe(false);
  return result.structuredContent;
}

/** The rows of the neighbour table: node id, edge type and direction. */
export async function neighbourRows(page: Page): Promise<{ node: string; rel: string; direction: string }[]> {
  return page.getByTestId("neighbour-row").evaluateAll((rows) =>
    rows.map((row) => ({
      node: row.getAttribute("data-node-id") ?? "",
      rel: row.getAttribute("data-rel") ?? "",
      direction: row.getAttribute("data-direction") ?? "",
    })),
  );
}

/**
 * Open `path` and wait until the streamed page has settled: Next.js streams the page into a hidden copy that React
 * removes a few hundred milliseconds after load, and until then a test id can match twice.
 */
export async function open(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.waitForFunction(() => document.querySelector('div[hidden][id^="S:"]') === null);
}

/** The canvas finished drawing (Cytoscape loaded in the browser and laid the graph out). */
export async function graphReady(page: Page): Promise<void> {
  await expect(page.getByTestId("kg-canvas")).toHaveAttribute("data-ready", "true");
}
