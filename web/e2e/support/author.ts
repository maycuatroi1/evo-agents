import { type ApiClient, call } from "../../src/lib/api/client";

import { API_URL } from "./env";
import { type Account, ADMIN_ACCOUNT, bearerClient, machineToken, registration } from "./hub";
import { type LiveWorker, liveWorker, MODELS, REPO, type Run } from "./runs";
import { packSkill, publishSkill } from "./skills";
import { PROTOCOL_HEADER } from "./workers";

/**
 * Author runs through the real API (docs/workers.md, Author runs): the global skill create-exec-plan an author run is
 * claimed with, published once as the hub admin; a worker of the member's whose daemon says it runs author runs and
 * has a checkout of the project's harness; and the worker's side of the run's chat, its waits and its plan, with the
 * worker's token, as the daemon does. The specs are the fake worker.
 */

/** The checkout name of the harness `registration()` gives every project of the stack: its path's last part. */
export const HARNESS_CHECKOUT = registration().harness?.path ?? "example-harness";
/** What a daemon of this release says it runs. */
export const RUN_KINDS = ["step", "plan", "review", "judge", "author"];

let skill: Promise<number> | null = null;

/** The hub's global create-exec-plan, which a claim of an author run hands the worker; published once per process. */
export function ensureAuthorSkill(): Promise<number> {
  skill ??= (async () => {
    const api = bearerClient(await machineToken(ADMIN_ACCOUNT));
    const bundle = await packSkill("create-exec-plan", "Write an execution plan. In an author run, ask in the chat.", "Write execution plans");
    return publishSkill(api, bundle, "create-exec-plan", null);
  })();
  return skill;
}

async function workerSend(live: LiveWorker, method: "POST" | "PUT", path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${method} ${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

/** The daemon's heartbeat: Claude Code with its models, checkouts of the harness and the project's repo, author runs. */
export async function authorHeartbeat(live: LiveWorker, runs: number[] = []): Promise<unknown> {
  return workerSend(live, "POST", "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289", models: MODELS } },
    checkouts: Object.fromEntries(
      [HARNESS_CHECKOUT, REPO].map((repo) => [`${live.project}/${repo}`, { path: `~/github/${repo}`, branch: "main" }]),
    ),
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
    run_kinds: RUN_KINDS,
  });
}

/** A worker of `account` serving `project` that takes author runs. */
export async function authorWorker(account: Account, project: string, name: string): Promise<LiveWorker> {
  const live = await liveWorker(account, project, name, 1, { report: { repos: [HARNESS_CHECKOUT, REPO], models: MODELS } });
  await authorHeartbeat(live);
  return live;
}

/** Queue an author run through the API as `account`, as `evo-agents hub run author` does. */
export async function dispatchAuthor(account: Account, project: string, workerId: number, request: string, planId: string | null = null): Promise<Run> {
  const api: ApiClient = bearerClient(await machineToken(account));
  return call(
    api.POST("/v1/projects/{project}/author-runs", {
      params: { path: { project } },
      body: { request, worker_id: workerId, plan_id: planId, runtime: "claude-code", timeout_h: 2 },
    }),
  );
}

/** The daemon's claim of the author run `runId` and the agent's start in its session. */
export async function startAuthor(live: LiveWorker, runId: number): Promise<void> {
  const answer = (await workerSend(live, "POST", "/v1/worker/claim", { wait_s: 0 })) as { run: { id: number } | null };
  if (answer.run?.id !== runId) throw new Error(`the worker claimed ${answer.run?.id ?? "nothing"}, not author run #${runId}`);
  await authorHeartbeat(live, [runId]);
  await workerSend(live, "POST", `/v1/worker/runs/${runId}/state`, { state: "running", session_id: "0199a3c1-0000-7000-8000-0000000000e2" });
}

/** The end of the agent's turn: its last message posted to the chat, then the run waiting for its owner's reply. */
export async function agentAsks(live: LiveWorker, runId: number, text: string): Promise<void> {
  await workerSend(live, "POST", `/v1/worker/runs/${runId}/chat`, { text });
  await workerSend(live, "POST", `/v1/worker/runs/${runId}/state`, { state: "waiting" });
}

/** The worker handing the reply to the agent: the run is running again. */
export async function agentResumes(live: LiveWorker, runId: number): Promise<void> {
  await authorHeartbeat(live, [runId]);
  await workerSend(live, "POST", `/v1/worker/runs/${runId}/state`, { state: "running" });
}

/** The agent's `evo-agents worker put`: the plan it wrote, on the hub as the run's owner. */
export async function agentPuts(live: LiveWorker, runId: number, body: Record<string, unknown>, ifRevision: number | null = null): Promise<{ revision: number }> {
  return (await workerSend(live, "PUT", `/v1/worker/runs/${runId}/plan`, { body, if_revision: ifRevision })) as { revision: number };
}

export type RunChat = { run_id: number; status: string; plan_id: string | null; plan_revision: number | null; messages: { author: string; text: string }[] };

/** The chat as `account` reads it through the API. */
export async function chatOf(account: Account, project: string, runId: number): Promise<RunChat> {
  const api = bearerClient(await machineToken(account));
  return (await call(api.GET("/v1/projects/{project}/runs/{run_id}/chat", { params: { path: { project, run_id: runId } } }))) as RunChat;
}
