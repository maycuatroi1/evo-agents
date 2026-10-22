import { readFileSync } from "node:fs";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { HARNESS_CHECKOUT } from "./author";
import { API_URL, STACK_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";
import { apiOf } from "./memories";
import { enablePlugin } from "./plugins";
import { type LiveWorker, liveWorker, MODELS, REPO } from "./runs";
import { PROTOCOL_HEADER } from "./workers";

/**
 * The core plugin srs through the real API, as `evo-agents hub srs import|submit|approve` does: the sample SRS of the
 * Python tests (tests/hub/fixtures/srs/demo-shop-srs.md, no client's data), imported, edited, submitted and reviewed
 * with a member's machine token, which needs no CSRF header.
 */
export type SrsVersion = components["schemas"]["SrsVersion"];
export type SrsChange = components["schemas"]["SrsChange"];
export type SrsFinding = components["schemas"]["SrsFinding"];

export const SAMPLE_SRS = readFileSync(new URL("../../../tests/hub/fixtures/srs/demo-shop-srs.md", import.meta.url), "utf8");
export const SAMPLE_SLUG = "demo-shop";
export const SAMPLE_TITLE = "Demo Shop: Order Service";
/** The sample's use case UC ORD-1, its first business rule, and a screen. */
export const USE_CASE = { code: "UC ORD-1", title: "Place an order" };
export const RULE = { code: "BR ORD1-1", title: "Cart rules" };
/** The second document of the findings specs (`importBilling`), and the rule of each that disagree on retries. */
export const BILLING_SLUG = "billing";
export const RETRIES = { code: "BR ORD2-1", billing: "BR BIL2-1" };

export function srsPath(project: string, rest = ""): string {
  return `/p/${project}/x/srs/documents${rest}`;
}

export function documentPath(project: string, rest = "", slug = SAMPLE_SLUG): string {
  return srsPath(project, `/${slug}${rest}`);
}

export function itemPath(project: string, code: string, slug = SAMPLE_SLUG): string {
  return documentPath(project, `/items/${encodeURIComponent(code)}`, slug);
}

/**
 * Import a second document, billing, whose codes are BIL and RFD where the sample's are ORD and RET (and its common
 * rules and NFRs numbered on), so its items are other items of the project: what @ba compares the sample with.
 */
export async function importBilling(account: Account, project: string): Promise<void> {
  let markdown = SAMPLE_SRS.replaceAll("ORD", "BIL").replaceAll("RET", "RFD");
  for (const [from, to] of [["CBR 1", "CBR 3"], ["CBR 2", "CBR 4"], ["SEC-01", "SEC-02"], ["PERF-01", "PERF-02"]]) markdown = markdown.replaceAll(from, to);
  const api = await apiOf(account);
  await call(
    api.POST("/v1/x/srs/{project}/import", {
      params: { path: { project } },
      body: { markdown, slug: BILLING_SLUG, kind: "srs", summary: "Imported", dry_run: false },
    }),
  );
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

// @ba, the agent type ba of srs: its review of a version in review, and the findings its agent records

/** What a daemon of this release runs, task runs among them. */
const BA_RUN_KINDS = ["step", "plan", "review", "judge", "author", "chat", "task"];
/**
 * What it says it does (`runs.WORKER_FEATURES`): @ba's runs attach the packs of srs and run in its environment, which the
 * hub hands only a daemon that builds packs and runs environments, and gives @ba's Claude Code agent a config dir of its
 * own (`runs.AGENT_HOME`). Its chat runs start with every repo it has.
 */
const BA_FEATURES = ["session_per_step", "author_decisions", "agent_profiles", "plugin_runtimes", "environments", "agent_packs", "chat_decisions", "agent_home"];

async function workerPost(live: LiveWorker, path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function baHeartbeat(live: LiveWorker, runs: number[] = []): Promise<void> {
  await workerPost(live, "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289", models: MODELS } },
    checkouts: Object.fromEntries([HARNESS_CHECKOUT, REPO].map((repo) => [`${live.project}/${repo}`, { path: `~/github/${repo}`, branch: "main" }])),
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
    run_kinds: BA_RUN_KINDS,
    features: BA_FEATURES,
  });
}

/**
 * @ba on in `project`, as its admin `admin` turns it on (`evo-agents hub agent-type enable srs/ba`): its runs go under
 * `runAs`, whose worker takes them, the packs of srs on it. Each version sent to review from now on hands @ba a review.
 * The handle ba is the hub's, deleted profiles included, so one project of a stack enables it: e2e/srs.spec.ts's. A page
 * that only needs a review with findings seeds them (`seedFindings`).
 */
export async function enableBa(admin: Account, project: string, runAs: Account): Promise<LiveWorker> {
  const api = await apiOf(admin);
  await call(
    api.PUT("/v1/projects/{project}/agent-types/{plugin}/{name}", {
      params: { path: { project, plugin: "srs", name: "ba" } },
      body: { run_as: runAs.login, daily_usd: 5 },
    }),
  );
  const live = await liveWorker(runAs, project, `ba-${project}`.slice(0, 40), 3, { report: { repos: [HARNESS_CHECKOUT, REPO], models: MODELS } });
  const owner = bearerClient(await machineToken(runAs));
  await call(owner.PATCH("/v1/workers/{worker_id}", { params: { path: { worker_id: live.worker.id } }, body: { plugins: ["srs"] } }));
  await baHeartbeat(live);
  return live;
}

/**
 * The daemon claims the run of @ba waiting for it, of `kind` (task: a review; chat: a turn in a settle chat), and starts
 * it: its id.
 */
export async function baStarts(live: LiveWorker, kind: "task" | "chat"): Promise<number> {
  const answer = (await workerPost(live, "/v1/worker/claim", { wait_s: 0 })) as { run: { id: number; kind: string } | null };
  if (!answer.run || answer.run.kind !== kind) throw new Error(`the worker claimed ${JSON.stringify(answer.run)}, not a ${kind} run`);
  const runId = answer.run.id;
  await baHeartbeat(live, [runId]);
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "running", session_id: `0199a3c1-0000-7000-8000-${String(runId).padStart(12, "0")}` });
  return runId;
}

/** The end of @ba's run: verifying, then done with its summary. */
export async function baEnds(live: LiveWorker, runId: number, summary: string): Promise<void> {
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "verifying" });
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "done", summary });
  await baHeartbeat(live);
}

/**
 * The agent of run `runId` calls srs__finding on the hub's /mcp, as Claude Code does through the worker, reading through
 * the stack's projects' agent sink (claude-code): its answer.
 */
export async function baFinding(live: LiveWorker, runId: number, args: Record<string, unknown>): Promise<string> {
  const response = await fetch(`${API_URL}/mcp`, {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      "mcp-protocol-version": "2025-11-25",
      authorization: `Bearer ${live.token}`,
      "x-evo-run": String(runId),
      "x-evo-sink": "claude-code",
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "srs__finding", arguments: { document: SAMPLE_SLUG, ...args } } }),
  });
  const answer = (await response.json()) as { result?: { content: { text: string }[]; isError: boolean } };
  const text = answer.result?.content[0]?.text ?? JSON.stringify(answer);
  if (!response.ok || !answer.result || answer.result.isError) throw new Error(`srs__finding ${String(args.action)}: ${text}`);
  return text;
}

/** The id of the finding the agent of run `runId` records with srs__finding (its answer begins "Finding <id> of"). */
export async function baRecords(live: LiveWorker, runId: number, finding: Record<string, unknown>): Promise<number> {
  const text = await baFinding(live, runId, { action: "record", ...finding });
  const id = /^Finding (\d+) of /.exec(text);
  if (!id) throw new Error(`srs__finding record answered: ${text}`);
  return Number(id[1]);
}

/** One pass of the hub's plugin deliveries for `project` (POST /plugins/deliver of the stack): it opens settle chats. */
export async function deliverPluginEvents(project: string): Promise<{ delivered: number; retried: number; failed: number }> {
  const response = await fetch(`${STACK_URL}/plugins/deliver`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project }),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`POST /plugins/deliver: ${response.status} ${text}`);
  return JSON.parse(text) as { delivered: number; retried: number; failed: number };
}

/** The findings of a document as `account` reads them. */
export async function findingsOf(account: Account, project: string, slug = SAMPLE_SLUG): Promise<SrsFinding[]> {
  const api = await apiOf(account);
  return call(api.GET("/v1/x/srs/{project}/documents/{slug}/findings", { params: { path: { project, slug } } }));
}

/** An admin of the project waives an open finding with `reason` (`evo-agents hub srs waive`). */
export async function waiveFinding(admin: Account, project: string, id: number, reason: string, slug = SAMPLE_SLUG): Promise<SrsFinding> {
  const api = await apiOf(admin);
  return call(
    api.POST("/v1/x/srs/{project}/documents/{slug}/findings/{finding_id}/waive", { params: { path: { project, slug, finding_id: id } }, body: { reason } }),
  );
}

/** What @ba found in demo-shop v1, as `seedFindings` leaves it: the open conflict to decide, its chat, an advice, a settled one. */
export type SeededFindings = { version: number; decide: number; advice: number; settled: number; chat: string };

export const DECIDE_SUMMARY = "Demo-shop retries a failed payment 3 times in 10 minutes, billing 5 times in 30.";

/**
 * demo-shop v1 in review with what @ba's review of it found, without @ba (whose handle one project of a stack holds):
 * billing and demo-shop imported by `author`, demo-shop sent to review, a chat of `author` and `settler`, and the
 * findings written as srs__finding writes them (POST /srs/findings of the stack): a contradiction to decide about the
 * retries, shown by billing's rule, open with the chat's link as a settle chat gives it; a duplicate to advise on; an
 * overlap `settler` settled in the chat. `words` puts other text in the conflict's summary and its source's note.
 */
export async function seedFindings(author: Account, project: string, settler: Account, words: { summary?: string; note?: string } = {}): Promise<SeededFindings> {
  await importBilling(author, project);
  const version = await importSample(author, project);
  await submitVersion(author, project, version);
  const api = await apiOf(author);
  const settle = await call(api.POST("/v1/chats", { body: { title: `Settle ${SAMPLE_SLUG} v${version}`, people: [settler.login], agents: [] } }));
  const chat = `/chats/${settle.id}`;
  const response = await fetch(`${STACK_URL}/srs/findings`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      project,
      slug: SAMPLE_SLUG,
      findings: [
        {
          kind: "contradiction",
          level: "decide",
          item: RETRIES.code,
          summary: words.summary ?? DECIDE_SUMMARY,
          sources: [{ kind: "code", ref: RETRIES.billing, note: words.note ?? "billing retries 5 times in 30 minutes" }],
          chat_link: chat,
        },
        { kind: "duplicate", level: "advisory", item: USE_CASE.code, summary: "Placing an order repeats billing's checkout.", sources: [{ kind: "code", ref: "UC BIL-1" }] },
        {
          kind: "overlap",
          level: "info",
          summary: "Both documents describe the cart.",
          sources: [{ kind: "code", ref: "BR BIL1-1" }],
          status: "resolved",
          resolution: "The overlap is fine: billing reads the cart.",
          settled_by: settler.login,
          chat_link: chat,
        },
      ],
    }),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`POST /srs/findings: ${response.status} ${text}`);
  const [decide, advice, settled] = (JSON.parse(text) as { ids: number[] }).ids;
  return { version, decide, advice, settled, chat };
}
