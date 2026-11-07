import { call } from "../../src/lib/api/client";

import { HARNESS_CHECKOUT } from "./author";
import { API_URL } from "./env";
import { ACCOUNT_POOL, type Account, bearerClient, machineToken } from "./hub";
import { apiOf } from "./memories";
import { enablePlugin } from "./plugins";
import { type LiveWorker, liveWorker, MODELS, REPO } from "./runs";
import { PROTOCOL_HEADER } from "./workers";

/**
 * @builder through the real API (evo_agents/core_plugins/builder): the core plugin enabled and its agent type turned on
 * in a project, as `evo-agents hub agent-type enable builder/builder` does, a worker of the admin a node of its pool,
 * and the agent of its chat run calling builder__propose on the hub's /mcp as Claude Code does through the worker. The
 * specs are the fake worker; no model is asked anything. The handle builder is the hub's, so one project of a stack
 * turns it on: e2e/agent-system-builder.spec.ts's.
 */
export const BUILDER = "builder";

const RUN_KINDS = ["step", "plan", "review", "judge", "author", "chat", "task"];
/** What the daemon says it does (`runs.WORKER_FEATURES`): @builder's runs attach its pack and have a config dir of their own. */
const FEATURES = ["session_per_step", "author_decisions", "agent_profiles", "plugin_runtimes", "environments", "agent_packs", "chat_decisions", "agent_home"];

async function workerPost(live: LiveWorker, path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function heartbeat(live: LiveWorker, runs: number[] = []): Promise<void> {
  await workerPost(live, "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289", models: MODELS } },
    checkouts: Object.fromEntries([HARNESS_CHECKOUT, REPO].map((repo) => [`${live.project}/${repo}`, { path: `~/github/${repo}`, branch: "main" }])),
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
    run_kinds: RUN_KINDS,
    features: FEATURES,
  });
}

/** @builder on in `project`, run as its admin `admin` with the project's account pool, and a worker of `admin` its node. */
export async function enableBuilder(admin: Account, project: string): Promise<LiveWorker> {
  await enablePlugin(admin, project, BUILDER, ["chats:read"]);
  const api = await apiOf(admin);
  const { handle } = await call(
    api.PUT("/v1/projects/{project}/agent-types/{plugin}/{name}", {
      params: { path: { project, plugin: BUILDER, name: BUILDER } },
      body: { run_as: admin.login, daily_usd: 5 },
    }),
  );
  await call(api.PUT("/v1/projects/{project}/agents/{handle}/account-pool", { params: { path: { project, handle } }, body: { account_pool: ACCOUNT_POOL } }));
  const live = await liveWorker(admin, project, `builder-${project}`.slice(0, 40), 2, { report: { repos: [HARNESS_CHECKOUT, REPO], models: MODELS }, agent: handle });
  const owner = bearerClient(await machineToken(admin));
  await call(owner.PATCH("/v1/workers/{worker_id}", { params: { path: { worker_id: live.worker.id } }, body: { plugins: [BUILDER] } }));
  await heartbeat(live);
  return live;
}

/** The daemon claims chat run `runId` of @builder and starts its agent. */
export async function builderStarts(live: LiveWorker, runId: number): Promise<void> {
  const answer = (await workerPost(live, "/v1/worker/claim", { wait_s: 0 })) as { run: { id: number; kind: string } | null };
  if (answer.run?.id !== runId) throw new Error(`the worker claimed ${JSON.stringify(answer.run)}, not chat run #${runId}`);
  await heartbeat(live, [runId]);
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "running", session_id: `0199a3c1-0000-7000-8000-${String(runId).padStart(12, "0")}` });
}

/** The agent of run `runId` proposes with builder__propose: the id of the proposal that waits. */
export async function builderProposes(live: LiveWorker, runId: number, args: Record<string, unknown>): Promise<number> {
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
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "builder__propose", arguments: args } }),
  });
  const answer = (await response.json()) as { result?: { content: { text: string }[]; isError: boolean } };
  const text = answer.result?.content[0]?.text ?? JSON.stringify(answer);
  const waits = /Proposal (\d+) waits for /.exec(text);
  if (!response.ok || !waits) throw new Error(`builder__propose: ${text}`);
  return Number(waits[1]);
}
