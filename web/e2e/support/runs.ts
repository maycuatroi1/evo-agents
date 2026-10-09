import { createHash } from "node:crypto";

import type { Page } from "@playwright/test";

import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";
import { HOST, PROTOCOL_HEADER } from "./workers";

/**
 * Plans with steps to run, workers that can take them, and the worker's side of the queue, all through the real API:
 * a plan pushed as a writer (`evo-agents hub plan import`), a worker registered from a signed-in machine
 * (`evo-agents worker register`), and the daemon's heartbeat, claim, state reports, events, inbox and diff upload
 * with its worker token (docs/workers.md). The specs are the fake worker: they emit what a daemon would.
 */
export type Run = components["schemas"]["Run"];
export type Worker = components["schemas"]["Worker"];

/** The plan the runs specs dispatch from. */
export const RUN_PLAN = "rollout";
/** Step 1 is done, 2 and 4 are ready, 3 waits for 2, and 5 is in progress. */
export const READY_STEPS = ["2", "4"];
export const WAITING_STEP = "3";
export const DONE_STEP = "1";
export const STEP_TITLES: Record<string, string> = {
  "1": "Run model and protocol",
  "2": "Queue with dispatch and claim",
  "3": "Worker daemon",
  "4": "Runs page on the web",
  "5": "Release notes",
};

/** The repo the plan's steps run in: the stack's projects register one repo, api. */
export const REPO = "api";

export function runPlanBody(id = RUN_PLAN) {
  const step = (key: number, extra: Record<string, unknown>) => ({
    id: key,
    title: STEP_TITLES[String(key)],
    repo: REPO,
    what: `Do step ${key}.`,
    verify: "pnpm test",
    ...extra,
  });
  return {
    id,
    title: "Worker fleet rollout",
    goal: "Run plan steps on workers.",
    repos: [{ repo: REPO, branch: "main", status: "pending" }],
    steps: [
      step(1, { status: "done", evidence: "api@abc1234: 40 tests pass" }),
      step(2, { status: "pending", depends_on: [1] }),
      step(3, { status: "pending", depends_on: [2] }),
      step(4, { status: "pending" }),
      step(5, { status: "in_progress" }),
    ],
  };
}

/** Push the runs plan into `project` as `writer`, who holds the writer role there. */
export async function seedRunPlan(writer: Account, project: string, id = RUN_PLAN): Promise<void> {
  const api = bearerClient(await machineToken(writer));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: id } },
      body: { body: runPlanBody(id), area: "active" },
    }),
  );
}

export type LiveWorker = { worker: Worker; token: string; project: string };

/**
 * A worker of `account` serving `project`, registered from a signed-in machine, after a first heartbeat that reports
 * Claude Code and a checkout of the plan's repo: a dispatch to `account` matches it. `terminal` registers it with
 * `--allow-web-terminal`.
 */
export async function liveWorker(
  account: Account,
  project: string,
  name: string,
  slots = 1,
  { terminal = false, report = {} }: { terminal?: boolean; report?: MachineReport } = {},
): Promise<LiveWorker> {
  const api = bearerClient(await machineToken(account));
  const credential = await call(
    api.POST("/v1/workers", {
      body: {
        ...HOST,
        hostname: `${name}.local`,
        name,
        projects: [project],
        slots,
        labels: [],
        allow_web_terminal: terminal,
      },
    }),
  );
  const live = { worker: credential.worker, token: credential.token, project };
  await workerHeartbeat(live, [], report);
  return live;
}

async function workerCall(live: LiveWorker, path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

/** What a heartbeat may say beyond the default: the repos checked out, and the models Claude Code lists. */
export type MachineReport = { repos?: string[]; models?: string[] | null };

/**
 * The daemon's heartbeat: Claude Code available (listing `models` when given), a checkout of each repo in `repos` (the
 * plan's repo by default), the runs it holds.
 */
export async function workerHeartbeat(live: LiveWorker, runs: number[] = [], report: MachineReport = {}): Promise<void> {
  const repos = report.repos ?? [REPO];
  await workerCall(live, "/v1/worker/heartbeat", {
    runtimes: {
      "claude-code": { available: true, version: "2.1.289", ...(report.models ? { models: report.models } : {}) },
      codex: { available: false, reason: "not found on PATH" },
    },
    checkouts: Object.fromEntries(repos.map((repo) => [`${live.project}/${repo}`, { path: `~/github/${repo}`, branch: "main" }])),
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
  });
}

/** What the worker holding a run gets from the hub: each lease with its value, and the repos no lease covers. */
export type Credentials = components["schemas"]["Credentials"];

/** The daemon asking for the leases of a run it holds, right after its claim. */
export async function leaseCredentials(live: LiveWorker, runId: number): Promise<Credentials> {
  return (await workerCall(live, `/v1/worker/runs/${runId}/credentials`, {})) as Credentials;
}

/** The daemon's claim, answered at once: the run it leased, or null. */
export async function claimRun(live: LiveWorker): Promise<{ id: number } | null> {
  const answer = (await workerCall(live, "/v1/worker/claim", { wait_s: 0 })) as { run: { id: number } | null };
  return answer.run;
}

/** The daemon reporting a move of a run it holds, such as {state: "failed", error}. */
export async function reportState(live: LiveWorker, runId: number, report: Record<string, unknown>): Promise<Run> {
  return (await workerCall(live, `/v1/worker/runs/${runId}/state`, report)) as Run;
}

/** Dispatch through the API as `account`, the way `evo-agents hub run` will. */
export async function dispatch(account: Account, project: string, steps: string[], extra: Record<string, unknown> = {}): Promise<Run[]> {
  const api: ApiClient = bearerClient(await machineToken(account));
  return call(
    api.POST("/v1/projects/{project}/runs", {
      params: { path: { project } },
      body: { plan_id: RUN_PLAN, steps, runtime: "any", mode: "headless", approval: "review", timeout_min: 60, ...extra },
    }),
  );
}

/** The project's runs as `account` reads them through the API, newest first. */
export async function runsOf(account: Account, project: string): Promise<Run[]> {
  const api = bearerClient(await machineToken(account));
  const list = await call(api.GET("/v1/projects/{project}/runs", { params: { path: { project }, query: { limit: 200 } } }));
  return list.runs;
}

/** The row of run `id` in a runs table. */
export function runRow(page: Page, id: number, table = "runs-table") {
  return page.locator("#main").getByTestId(table).locator("tbody tr").filter({ has: page.locator(`[data-run-id="${id}"]`) });
}

/** The daemon's report that the agent started, with its session id. */
export async function startRun(live: LiveWorker, runId: number, sessionId = "3f2a9c1e-0000-4000-8000-000000000001"): Promise<Run> {
  await workerHeartbeat(live, [runId]);
  return reportState(live, runId, { state: "running", session_id: sessionId });
}

/** A commit name as the hub takes it: 40 hex digits. */
export const COMMIT = "7c1e9a2f3b4c5d6e7f8091a2b3c4d5e6f7081920";

/** The daemon's reports after the agent finished: verifying, then review with what it verified. */
export async function runToReview(live: LiveWorker, runId: number): Promise<Run> {
  await reportState(live, runId, { state: "verifying" });
  return reportState(live, runId, {
    state: "review",
    commit_sha: COMMIT,
    diffstat: { files: 2, insertions: 12, deletions: 3 },
    verify: [{ command: "pnpm test", exit_code: 0, duration_ms: 1400 }],
    usage: { input_tokens: 41200, output_tokens: 6800 },
    summary: "Added the queue with dispatch and claim.",
  });
}

export type WorkerEvent = { kind: string; body: Record<string, unknown>; at?: string };

/**
 * Events of a run as the daemon sends them: numbered on from what the hub acknowledged (an empty batch asks), in
 * batches of at most 500. Answers the hub's ack_seq.
 */
export async function sendEvents(live: LiveWorker, runId: number, events: WorkerEvent[]): Promise<number> {
  const path = `/v1/worker/runs/${runId}/events`;
  let { ack_seq: seq } = (await workerCall(live, path, { events: [] })) as { ack_seq: number };
  for (let start = 0; start < events.length; start += 500) {
    const batch = events.slice(start, start + 500).map((event) => ({
      seq: ++seq,
      at: event.at ?? new Date().toISOString(),
      kind: event.kind,
      body: event.body,
    }));
    const answer = (await workerCall(live, path, { events: batch })) as { ack_seq: number };
    seq = answer.ack_seq;
  }
  return seq;
}

/** What the agent says, as one event. */
export function say(text: string): WorkerEvent {
  return { kind: "agent_message_chunk", body: { text } };
}

/** A tool the agent ran, as one event. */
export function tool(title: string, command: string): WorkerEvent {
  return { kind: "tool_call", body: { toolCallId: `call-${command.length}`, title, rawInput: { command }, status: "in_progress" } };
}

/** The messages waiting in the run's inbox, as the worker holding it takes them (without acknowledging any). */
export async function workerInbox(live: LiveWorker, runId: number): Promise<{ id: number; text: string; sent_by: string }[]> {
  const answer = (await workerCall(live, `/v1/worker/runs/${runId}/inbox`, {})) as { messages: { id: number; text: string; sent_by: string }[] };
  return answer.messages;
}

/** The daemon's heartbeat answer for one run: what the owner asked of it. */
export async function runControl(live: LiveWorker, runId: number): Promise<Record<string, unknown>> {
  const answer = (await workerCall(live, "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289" } },
    checkouts: { [`${live.project}/${REPO}`]: { path: `~/github/${REPO}`, branch: "main" } },
    free_slots: 0,
    runs: [runId],
  })) as { runs: Record<string, unknown>[] };
  return answer.runs.find((run) => run.id === runId) ?? {};
}

/** The run's diff, uploaded as the daemon does when the run ends: ask, PUT to the presigned URL, commit. */
export async function uploadDiff(live: LiveWorker, runId: number, text: string): Promise<string> {
  const data = Buffer.from(text, "utf8");
  const sha256 = createHash("sha256").update(data).digest("hex");
  const asked = (await workerCall(live, `/v1/worker/runs/${runId}/uploads`, {
    items: [{ sha256, size: data.length, kind: "run-diff" }],
  })) as { uploads: { upload_id: string; url: string }[] };
  for (const ticket of asked.uploads) {
    const put = await fetch(ticket.url, { method: "PUT", body: new Uint8Array(data), headers: { "content-type": "application/octet-stream" } });
    if (!put.ok) throw new Error(`PUT to the presigned URL: ${put.status}`);
  }
  if (asked.uploads.length) {
    await workerCall(live, `/v1/worker/runs/${runId}/blobs`, { upload_ids: asked.uploads.map((ticket) => ticket.upload_id) });
  }
  return sha256;
}

/** A run as `account` reads it through the API. */
export async function runOf(account: Account, project: string, id: number): Promise<Run> {
  const api = bearerClient(await machineToken(account));
  return call(api.GET("/v1/projects/{project}/runs/{run_id}", { params: { path: { project, run_id: id } } }));
}

/** A run's events as `account` reads them through the API. */
export async function eventsOf(account: Account, project: string, id: number) {
  const api = bearerClient(await machineToken(account));
  return call(api.GET("/v1/projects/{project}/runs/{run_id}/events", { params: { path: { project, run_id: id }, query: { limit: 1000 } } }));
}

/** The run's page. */
export function runPath(project: string, id: number): string {
  return `/p/${project}/runs/${id}`;
}

// Plan runs: one run that does every step of a plan not done yet (docs/workers.md, A plan run on the machine).

/** The plan the plan-run specs run: four steps over two repos, step 1 done, step 3 a checkpoint. */
export const PLAN_RUN_PLAN = "fleet";
/** The second repo of the plan run, on its default branch main. */
export const HARNESS_REPO = "harness";
export const PLAN_RUN_BRANCH = "feat/plan-runs";
export const PLAN_RUN_TITLES: Record<string, string> = {
  "1": "Plan run model",
  "2": "Run plan dialog",
  "3": "Checkpoint: deploy to staging",
  "4": "Update the use-case catalog",
};
/** The models the plan-run worker's Claude Code lists. */
export const MODELS = ["claude-opus-4-1", "claude-sonnet-4-5"];

export function planRunBody(id = PLAN_RUN_PLAN, { allDone = false }: { allDone?: boolean } = {}) {
  const step = (key: number, repo: string, extra: Record<string, unknown>) => ({
    id: key,
    title: PLAN_RUN_TITLES[String(key)],
    repo,
    what: `Do step ${key}.`,
    verify: "pnpm test",
    ...(allDone ? { status: "done", evidence: `step ${key} verified` } : {}),
    ...extra,
  });
  return {
    id,
    title: "Plan runs on the web",
    goal: "Run a whole plan on one worker.",
    repos: [
      { repo: REPO, branch: PLAN_RUN_BRANCH, status: "in_progress" },
      { repo: HARNESS_REPO, branch: "main", status: "pending" },
    ],
    steps: [
      step(1, REPO, { status: "done", evidence: "api@abc1234: 40 tests pass" }),
      step(2, REPO, { status: allDone ? "done" : "pending", depends_on: [1] }),
      step(3, REPO, { status: allDone ? "done" : "pending", depends_on: [2] }),
      step(4, HARNESS_REPO, { status: allDone ? "done" : "pending", depends_on: [3] }),
    ],
  };
}

/** Push the plan-run plan into `project` as `writer`. */
export async function seedPlanRunPlan(writer: Account, project: string, id = PLAN_RUN_PLAN, options: { allDone?: boolean } = {}): Promise<void> {
  const api = bearerClient(await machineToken(writer));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: id } },
      body: { body: planRunBody(id, options), area: "active" },
    }),
  );
}

/** A worker with checkouts of both repos of the plan run, whose Claude Code lists MODELS; `terminal` as liveWorker's. */
export function planRunWorker(account: Account, project: string, name: string, { terminal = false }: { terminal?: boolean } = {}): Promise<LiveWorker> {
  return liveWorker(account, project, name, 1, { terminal, report: { repos: [REPO, HARNESS_REPO], models: MODELS } });
}

/** Dispatch a plan run through the API as `account`, the way `evo-agents hub run plan` does. */
export async function dispatchPlan(account: Account, project: string, extra: Record<string, unknown> = {}): Promise<Run> {
  const api: ApiClient = bearerClient(await machineToken(account));
  return call(
    api.POST("/v1/projects/{project}/plan-runs", {
      params: { path: { project } },
      body: { plan_id: PLAN_RUN_PLAN, runtime: "any", mode: "headless", timeout_h: 4, ...extra },
    }),
  );
}

/** The agent's `evo-agents worker step KEY in_progress`, through the worker's token. */
export async function reportStep(live: LiveWorker, runId: number, key: string, status: "in_progress" | "pending", repo = REPO): Promise<void> {
  await workerCall(live, `/v1/worker/runs/${runId}/steps/${key}`, { status, repo });
}

/** What an agent's ask may say beyond the default: its category, context, options and pick. */
export type DecisionAsk = {
  category?: string;
  context?: string | null;
  options?: { key: string; label: string; description?: string }[];
  recommended?: string | null;
};

/** The agent's `evo-agents worker ask`: a decision of the run, answered by its owner. Returns its id. */
export async function askDecision(live: LiveWorker, runId: number, question: string, step = "3", ask: DecisionAsk = {}): Promise<number> {
  const decision = (await workerCall(live, `/v1/worker/runs/${runId}/decisions`, {
    category: "deploy",
    question,
    context: "The checkpoint deploys **staging**.",
    options: [
      { key: "deploy", label: "Deploy to staging now" },
      { key: "wait", label: "Wait until tomorrow" },
    ],
    recommended: "deploy",
    step_key: step,
    ...ask,
  })) as { id: number };
  return decision.id;
}

/**
 * A plan run the worker took and started, with step 2 in progress; with `waiting`, its agent then asked a decision and
 * the run waits for the answer. `terminal` registers its worker with `--allow-web-terminal`. Returns the run and the
 * decision's id, if any.
 */
export async function planRunUnderway(
  owner: Account,
  project: string,
  name: string,
  {
    waiting = false,
    dispatch = {},
    terminal = false,
  }: { waiting?: boolean; dispatch?: Record<string, unknown>; terminal?: boolean } = {},
): Promise<{ live: LiveWorker; run: Run; decision: number | null }> {
  const live = await planRunWorker(owner, project, name, { terminal });
  const run = await dispatchPlan(owner, project, { worker_id: live.worker.id, runtime: "claude-code", model: MODELS[1], ...dispatch });
  const claimed = await claimRun(live);
  if (claimed?.id !== run.id) throw new Error(`the worker claimed ${claimed?.id ?? "nothing"}, not plan run #${run.id}`);
  await workerHeartbeat(live, [run.id], { repos: [REPO, HARNESS_REPO], models: MODELS });
  await reportState(live, run.id, { state: "running", session_id: "3f2a9c1e-0000-4000-8000-0000000000aa" });
  await reportStep(live, run.id, "2", "in_progress");
  let decision: number | null = null;
  if (waiting) {
    decision = await askDecision(live, run.id, "Deploy the plan-runs build to staging now?");
    await reportState(live, run.id, { state: "waiting" });
  }
  return { live, run: await runOf(owner, project, run.id), decision };
}

// The Inbox: notices of a plan run, and answers through the API (docs/notifications.md).

/** A notice of a push or merge, as `evo-agents worker notify` sends it. */
export type NoticeIn = {
  kind: "push_default_branch" | "merge_default_branch" | "plan_finished" | "run_failed";
  title: string;
  body?: string;
  repo?: string;
  branch?: string;
  commits?: string[];
};

/** The worker's notice for the owner of a plan run it holds. Returns the notification's id. */
export async function sendNotice(live: LiveWorker, runId: number, notice: NoticeIn): Promise<number> {
  const answer = (await workerCall(live, `/v1/worker/runs/${runId}/notices`, notice)) as { id: number };
  return answer.id;
}

/** The member's unread notifications, open decisions and open proposals, as the bell reads them. */
export async function notificationCount(account: Account): Promise<components["schemas"]["NotificationCount"]> {
  const api = bearerClient(await machineToken(account));
  return call(api.GET("/v1/me/notifications/count"));
}

/** Answer a decision through the API as `account`, the way `evo-agents hub decision answer` does; the HTTP status. */
export async function answerByApi(account: Account, project: string, decision: number, body: { option?: string; text?: string }): Promise<number> {
  const token = await machineToken(account);
  const response = await fetch(`${API_URL}/v1/projects/${project}/decisions/${decision}/answer`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  return response.status;
}

/** A decision as `account` reads it through the API. */
export async function decisionOf(account: Account, project: string, decision: number) {
  const api = bearerClient(await machineToken(account));
  return call(api.GET("/v1/projects/{project}/decisions/{decision_id}", { params: { path: { project, decision_id: decision } } }));
}
