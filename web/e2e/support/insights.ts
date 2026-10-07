import { call } from "../../src/lib/api/client";

import { API_URL, STACK_URL } from "./env";
import { type Account, bearerClient, machineToken, uniqueName } from "./hub";
import { claimRun, dispatch, liveWorker, REPO, reportState, RUN_PLAN, startRun } from "./runs";

/**
 * Runs that ended on the last three UTC days, for the Insights specs: each one dispatched, claimed, started and ended
 * through the real API as a worker daemon does, then moved back to its day by the stack (POST /runs/backdate), which
 * also fixes how long it ran. A run that ends done marks its step done, so the plan has a step for each run.
 */

/** Usage as Claude Code reports it: 8,000 read from the cache, 1,200 input, 600 output of which 100 thinking. */
const CLAUDE_USAGE = { input_tokens: 1200, cache_read_input_tokens: 8000, output_tokens: 600, output_tokens_details: { thinking_tokens: 100 } };
/** Usage as Codex reports it: 3,000 input of which 1,000 cached, 700 output of which 200 reasoning. */
const CODEX_USAGE = { input_tokens: 3000, cached_input_tokens: 1000, output_tokens: 700, reasoning_output_tokens: 200 };

type Outcome = "done" | "failed" | "cancelled" | "lost";
type Ending = { step: string; outcome: Outcome; daysAgo: number; seconds: number; usage?: Record<string, unknown> };

/** In the order they run; the lost one last, since its worker is revoked to lose it. */
export const INSIGHT_RUNS: readonly Ending[] = [
  { step: "1", outcome: "done", daysAgo: 2, seconds: 120, usage: CLAUDE_USAGE },
  { step: "2", outcome: "done", daysAgo: 2, seconds: 600, usage: CLAUDE_USAGE },
  { step: "3", outcome: "failed", daysAgo: 2, seconds: 300, usage: CODEX_USAGE },
  { step: "4", outcome: "done", daysAgo: 1, seconds: 60 },
  { step: "5", outcome: "cancelled", daysAgo: 1, seconds: 30 },
  { step: "6", outcome: "failed", daysAgo: 0, seconds: 90 },
  { step: "7", outcome: "lost", daysAgo: 0, seconds: 150 },
];

/**
 * What the page shows of those runs, by days ago: the counts by outcome, the failure rate (failed or lost of done,
 * failed or lost), the median and 90th percentile (Postgres' percentile_cont) of how long they ran, and the tokens
 * as the usage card reads them. `total` is the whole range.
 */
export const EXPECTED = {
  2: {
    outcomes: ["2", "1", "0", "0", "3"],
    failure: ["33%", "1", "3"],
    duration: ["5m 0s", "9m 0s"],
    tokens: ["17,000", "4,400", "1,500", "400", "23,300"],
  },
  1: {
    outcomes: ["1", "0", "0", "1", "2"],
    failure: ["0%", "0", "1"],
    duration: ["45s", "57s"],
    tokens: ["0", "0", "0", "0", "0"],
  },
  0: {
    outcomes: ["0", "1", "1", "0", "2"],
    failure: ["100%", "2", "2"],
    duration: ["2m 0s", "2m 24s"],
    tokens: ["0", "0", "0", "0", "0"],
  },
  total: {
    outcomes: ["3", "2", "1", "1", "7"],
    failure: ["50%", "3", "6"],
    duration: ["2m 0s", "7m 0s"],
    tokens: ["17,000", "4,400", "1,500", "400", "23,300"],
  },
} as const;

/** The UTC day `daysAgo` days before today, as the API writes it ("2026-10-05"). */
export function utcDay(daysAgo: number): string {
  return new Date(Date.now() - daysAgo * 86_400_000).toISOString().slice(0, 10);
}

/** The Insights plan: eight steps of the runs repo, none depending on another, all pending. */
export function insightsPlanBody(id = RUN_PLAN) {
  return {
    id,
    title: "Insights fixtures",
    goal: "End runs on earlier days.",
    repos: [{ repo: REPO, branch: "main", status: "pending" }],
    steps: Array.from({ length: 8 }, (_, index) => ({
      id: index + 1,
      title: `Fixture step ${index + 1}`,
      repo: REPO,
      what: `Do step ${index + 1}.`,
      verify: "pnpm test",
      status: "pending",
    })),
  };
}

/** Move ended run `runId` `days` UTC days back; it ran `seconds` from start to end. */
export async function backdateRun(runId: number, days: number, seconds: number): Promise<void> {
  const response = await fetch(`${STACK_URL}/runs/backdate`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ run_id: runId, days, seconds }),
  });
  if (!response.ok) throw new Error(`hub_stack /runs/backdate: ${response.status} ${await response.text()}`);
}

/** The owner revoking their worker: the hub releases the runs it holds, and one not pinned to it is lost. */
async function revokeWorker(owner: Account, workerId: number): Promise<void> {
  const response = await fetch(`${API_URL}/v1/workers/${workerId}/revoke`, {
    method: "POST",
    headers: { authorization: `Bearer ${await machineToken(owner)}` },
  });
  if (!response.ok) throw new Error(`revoke worker ${workerId}: ${response.status} ${await response.text()}`);
}

/** INSIGHT_RUNS in `project`, which `owner` writes to, through a worker of theirs that ends revoked. */
export async function seedInsights(owner: Account, project: string): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: RUN_PLAN } },
      body: { body: insightsPlanBody(), area: "active" },
    }),
  );
  const live = await liveWorker(owner, project, uniqueName("insights"));
  for (const ending of INSIGHT_RUNS) {
    const [run] = await dispatch(owner, project, [ending.step], { approval: "auto" });
    const claimed = await claimRun(live);
    if (claimed?.id !== run.id) throw new Error(`the worker claimed ${claimed?.id ?? "nothing"}, not run #${run.id}`);
    await startRun(live, run.id);
    if (ending.outcome === "done") {
      await reportState(live, run.id, { state: "verifying" });
      await reportState(live, run.id, {
        state: "done",
        verify: [{ command: "pnpm test", exit_code: 0, duration_ms: 1200 }],
        ...(ending.usage ? { usage: ending.usage } : {}),
      });
    } else if (ending.outcome === "failed") {
      await reportState(live, run.id, { state: "failed", error: "verify failed: pnpm test exited 1", ...(ending.usage ? { usage: ending.usage } : {}) });
    } else if (ending.outcome === "cancelled") {
      await reportState(live, run.id, { state: "cancelled" });
    } else {
      await revokeWorker(owner, live.worker.id);
    }
    await backdateRun(run.id, ending.daysAgo, ending.seconds);
  }
}
