import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL, STACK_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";
import { claimRun, type LiveWorker, liveWorker, reportState, REPO } from "./runs";
import { PROTOCOL_HEADER } from "./workers";

/**
 * A night of a project's Curator through the real API, the way the hub and a worker daemon live it: an admin writes
 * the charter (its window around now, in UTC), the session's digest is pushed as the Stop hook pushes it, the stack
 * runs one pass of the job curator.collect (hub_stack POST /curator/collect) so the night's review run is queued, and
 * the spec plays the worker on duty: it claims the run and records what its agent would, a finding and four proposals
 * of each tier with evidence the hub resolves, then reports it done, which puts the tier 2 proposal in the owner's
 * Inbox.
 */
export type Charter = components["schemas"]["Charter"];
export type Proposal = components["schemas"]["Proposal"];

/** The session whose digest the evidence cites. */
export const SESSION = "0199a3c1-0000-7000-8000-00000000e2e0";
/** What the digest's first error says, as the proposal page shows the entry. */
export const BLOCKED = "Blocked: sleep 30 followed by tail is not allowed";

/** The proposals a night records, by tier, with their titles. */
export const TITLES = {
  0: "Bring the README's install command up to date",
  1: "Fold the claim retries into one helper",
  2: "A wait helper instead of sleep and tail",
  3: "Retry the deploy when the registry answers 503",
} as const;

const two = (n: number) => String(n).padStart(2, "0");

/** A window of five hours around now, in UTC, so the night is on whatever the hour. */
export function windowAroundNow(): { start: string; end: string; timezone: string } {
  const hour = new Date().getUTCHours();
  return { start: `${two((hour + 22) % 24)}:00`, end: `${two((hour + 3) % 24)}:00`, timezone: "UTC" };
}

export function charterBody(worker: string, extra: Record<string, unknown> = {}) {
  return {
    goals: [{ id: "night-shift", what: "Run the plans the owner approved while the owner sleeps." }],
    window: windowAroundNow(),
    worker,
    night_budget_usd: 2,
    run_budget_usd: 0.5,
    run_max_turns: 40,
    run_minutes: 30,
    max_runs_per_night: 3,
    night_plans: [],
    max_decisions_per_day: 5,
    brief_at: "06:30",
    auto_merge: [0],
    protected_paths: ["deploy/**", ".github/workflows/**"],
    judge: { runtime: "codex", model: null, hidden_checks: ["python -m pytest -q tests/hidden"] },
    ...extra,
  };
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

async function workerPost<T>(live: LiveWorker, path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** The daemon's heartbeat saying it runs review runs, with a checkout of the project's repo. */
export async function reviewHeartbeat(live: LiveWorker, runs: number[] = []): Promise<void> {
  await workerPost(live, "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289" }, codex: { available: false, reason: "not found on PATH" } },
    checkouts: { [`${live.project}/${REPO}`]: { path: `~/github/${REPO}`, branch: "main" } },
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
    agent_version: "0.8.0",
    run_kinds: ["step", "plan", "review"],
  });
}

/** A worker of `owner` on duty for `project`: registered, live, and running review runs. */
export async function dutyWorker(owner: Account, project: string, name: string): Promise<LiveWorker> {
  const live = await liveWorker(owner, project, name);
  await reviewHeartbeat(live);
  return live;
}

/** The charter `owner` (an admin of `project`) writes through the API, naming `worker`. */
export async function writeCharter(owner: Account, project: string, worker: string, extra: Record<string, unknown> = {}): Promise<Charter> {
  const api = bearerClient(await machineToken(owner));
  return call(api.PUT("/v1/projects/{project}/curator/charter", { params: { path: { project } }, body: charterBody(worker, extra) as never }));
}

/** The digest of a session of `owner` in `project`, with a blocked command, a 503 and a correction. */
export async function pushDigest(owner: Account, project: string): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  await call(
    api.PUT("/v1/projects/{project}/digests/{session_id}", {
      params: { path: { project, session_id: SESSION } },
      body: {
        cwd: "/src/example-harness",
        messages: 12,
        model: "claude-opus-5-5",
        tools: [{ "gen_ai.tool.name": "Bash", calls: 40, errors: 6 }],
        bash: [{ program: "sleep", calls: 6, errors: 3 }],
        user_turns: ["fix the claim retries", "no, stop using sleep then tail"],
        commands: ["sleep 30", "tail -n 20 worker.log"],
        repeated_commands: [{ command: "pnpm test", n: 4 }],
        errors: [
          { "gen_ai.tool.name": "Bash", text: BLOCKED, n: 3 },
          { "gen_ai.tool.name": "Bash", text: "claim failed: the hub answered HTTP 503", n: 2 },
        ],
      } as never,
    }),
  );
}

/** One pass of curator.collect on the stack: the night's figures, and its review run queued. */
export async function collect(): Promise<Record<string, number>> {
  return stack("/curator/collect", {});
}

const DRAFT = {
  id: "curator-wait-helper",
  title: "A wait helper instead of sleep and tail",
  goal: "Agents wait on background work without a blocked command.",
  steps: [
    {
      id: 1,
      title: "Wait helper",
      repo: REPO,
      what: "A helper that waits on a file without sleep and tail.",
      verify: "pnpm test -- wait",
      acceptance: ["no session of the next week has a blocked sleep followed by tail"],
    },
  ],
};

export type Night = { live: LiveWorker; runId: number; findingId: number; proposals: Record<0 | 1 | 2 | 3, Proposal> };

/**
 * The night's review run of `project`, queued by curator.collect, claimed by `live`, with a finding and a proposal of
 * each tier recorded, then done. Its tier 2 proposal reaches the Inbox of the charter's owner.
 */
export async function reviewNight(live: LiveWorker, { finish = true }: { finish?: boolean } = {}): Promise<Night> {
  await collect();
  const claimed = await claimRun(live);
  if (!claimed) throw new Error("the night's review run was not queued for this worker");
  const runId = claimed.id;
  await reviewHeartbeat(live, [runId]);
  await reportState(live, runId, { state: "running", session_id: "3f2a9c1e-0000-4000-8000-0000000000aa" });
  const finding = await workerPost<{ id: number }>(live, `/v1/worker/runs/${runId}/findings`, {
    lens: "environment",
    severity: "high",
    title: "The harness blocks sleep followed by tail",
    body: "Three sessions this week waited on a log with `sleep 30` and `tail`, and the harness **blocked** each one.",
    evidence: [
      { kind: "session", session_id: SESSION, field: "errors", index: 0 },
      { kind: "run", run_id: runId, seq: 1 },
    ],
  });
  const propose = (body: Record<string, unknown>) =>
    workerPost<Proposal>(live, `/v1/worker/runs/${runId}/proposals`, { plan: DRAFT, ...body });
  const tier2 = await propose({
    lens: "environment",
    kind: "feature",
    title: TITLES[2],
    summary: "Add `wait_for` to the worker so agents stop blocking on **sleep then tail**.",
    paths: [{ repo: REPO, path: "src/wait.py" }],
    finding_ids: [finding.id],
    evidence: [{ kind: "code", repo: REPO, path: "src/worker.py", line: 42 }],
  });
  const tier0 = await propose({
    lens: "docs_drift",
    kind: "docs",
    title: TITLES[0],
    summary: "The README still says `pip install evo-ak==0.3`.",
    paths: [{ repo: REPO, path: "README.md" }],
    evidence: [{ kind: "run", run_id: runId, seq: 1 }],
  });
  const tier1 = await propose({
    lens: "tool_errors",
    kind: "refactor",
    title: TITLES[1],
    paths: [{ repo: REPO, path: "src/claim.py" }],
    evidence: [{ kind: "session", session_id: SESSION, field: "user_turns", index: 0 }],
  });
  const tier3 = await propose({
    lens: "security",
    kind: "fix",
    title: TITLES[3],
    paths: [{ repo: REPO, path: "deploy/prod.yaml" }],
    evidence: [{ kind: "session", session_id: SESSION, field: "errors", index: 1 }],
  });
  if (finish) {
    await reportState(live, runId, { state: "verifying" });
    await reportState(live, runId, { state: "done" });
  }
  return { live, runId, findingId: finding.id, proposals: { 0: tier0, 1: tier1, 2: tier2, 3: tier3 } };
}

/** Everything a Curator spec starts from: the charter of `owner`'s project on their duty worker, a digest, a night. */
export async function seedCurator(owner: Account, project: string, name: string): Promise<Night> {
  const live = await dutyWorker(owner, project, name);
  await writeCharter(owner, project, live.worker.name);
  await pushDigest(owner, project);
  return reviewNight(live);
}
