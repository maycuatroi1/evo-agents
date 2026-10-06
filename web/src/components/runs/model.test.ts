import { describe, expect, it } from "vitest";

import type { Worker } from "@/components/workers/queries";
import { parsePlan } from "@/lib/plans";

import {
  activePlanRun,
  dispatchOutlook,
  dispatchWorkers,
  durationParts,
  facetCount,
  filtersSearch,
  fitWorker,
  isCheckpoint,
  listQuery,
  modelSuggestions,
  planRunLock,
  planRunPhase,
  planRunScope,
  readFilters,
  readModel,
  readySelection,
  runTiming,
  runtimeAvailable,
  runtimeCount,
  selectedRepos,
  splitSteps,
  summarizeRuns,
  workerRuntimes,
} from "./model";
import { hasActiveRuns, type Run, type RunList, type StateCounts, type StepReadiness } from "./queries";

const ZERO: StateCounts = {
  queued: 0,
  leased: 0,
  running: 0,
  interactive: 0,
  verifying: 0,
  waiting: 0,
  review: 0,
  parked: 0,
  done: 0,
  failed: 0,
  lost: 0,
  cancelled: 0,
};

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 1,
    kind: "step",
    project: "demo",
    plan_id: "rollout",
    step_key: "2",
    title: "Queue",
    plan_revision: 3,
    dispatched_by: "octo",
    worker_id: null,
    worker: null,
    pinned_worker_id: null,
    requested_runtime: "any",
    runtime: "any",
    mode: "headless",
    approval: "review",
    timeout_min: 60,
    attempt: 1,
    max_attempts: 3,
    parent_run_id: null,
    state: "queued",
    lease_expires_at: null,
    session_id: null,
    repo: "api",
    branch: null,
    commit_sha: null,
    diffstat: null,
    verify: null,
    evidence: null,
    usage: null,
    error: null,
    last_seq: 0,
    cancel_requested_at: null,
    takeover_requested_at: null,
    handback_requested_at: null,
    queued_at: "2026-10-05T07:00:00Z",
    leased_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  } as Run;
}

function worker(overrides: Partial<Worker> = {}): Worker {
  return {
    id: 7,
    name: "lab-ws-01",
    owner: "octo",
    status: "online",
    hostname: "lab-ws-01.local",
    os: "Linux",
    arch: "x86_64",
    agent_version: "0.3.0",
    slots: 2,
    labels: [],
    projects: ["demo"],
    runtimes: { "claude-code": { available: true, version: "2.1.289" }, codex: { available: false, reason: "not found" } },
    checkouts: { "demo/api": { path: "~/github/api", branch: "main" } },
    free_slots: 2,
    allow_web_terminal: false,
    held_runs: 0,
    created_at: "2026-10-05T07:00:00Z",
    last_heartbeat_at: "2026-10-05T07:10:00Z",
    drained_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function step(key: string, overrides: Partial<StepReadiness> = {}): StepReadiness {
  return { key, title: `Step ${key}`, repo: "api", status: "pending", ready: true, reason: null, active_run: null, ...overrides };
}

const params = (text: string) => new URLSearchParams(text);

describe("filters", () => {
  it("reads the facet, search and page from the URL, ignoring what the API would refuse", () => {
    expect(readFilters(params("state=active&q=%20queue%20&page=3"))).toEqual({ facet: "active", q: "queue", page: 3 });
    expect(readFilters(params("state=running&page=0"))).toEqual({ facet: null, q: "", page: 1 });
    expect(readFilters(params(`q=${"x".repeat(300)}&page=1.5`)).q).toHaveLength(200);
    expect(readFilters(params("page=999999")).page).toBe(1);
  });

  it("writes only what is set, and pages from 2 on", () => {
    expect(filtersSearch({ facet: null, q: "", page: 1 })).toBe("");
    expect(filtersSearch({ facet: "ended", q: "lab ws", page: 2 })).toBe("?state=ended&q=lab+ws&page=2");
  });

  it("asks the API for the states of the facet, one page at a time", () => {
    expect(listQuery({ facet: "ended", q: "x", page: 3 })).toEqual({
      states: ["failed", "lost", "cancelled"],
      q: "x",
      limit: 50,
      offset: 100,
    });
    expect(listQuery({ facet: null, q: "", page: 1 }).states).toEqual([]);
  });

  it("counts a facet from the counts of its states", () => {
    const counts = { ...ZERO, queued: 2, running: 1, interactive: 1, review: 1, done: 4, failed: 1, lost: 2 };
    expect(facetCount(counts, "active")).toBe(4);
    expect(facetCount({ ...counts, waiting: 1, parked: 1 }, "active")).toBe(6);
    expect(facetCount(counts, "ended")).toBe(3);
    expect(facetCount(counts, null)).toBe(12);
  });
});

describe("refresh", () => {
  it("is live while any run is queued, held or in review", () => {
    expect(hasActiveRuns(undefined)).toBe(false);
    expect(hasActiveRuns({ counts: { ...ZERO, done: 3, failed: 1 } })).toBe(false);
    expect(hasActiveRuns({ counts: { ...ZERO, review: 1 } })).toBe(true);
    expect(hasActiveRuns({ counts: { ...ZERO, verifying: 1 } })).toBe(true);
    expect(hasActiveRuns({ counts: { ...ZERO, waiting: 1 } })).toBe(true);
    expect(hasActiveRuns({ counts: { ...ZERO, parked: 1 } })).toBe(true);
  });
});

describe("summarizeRuns", () => {
  it("counts held runs and the workers holding them, the oldest queued run and the one in review", () => {
    const list: RunList = {
      runs: [
        run({ id: 9, state: "running", worker_id: 1 }),
        run({ id: 8, state: "leased", worker_id: 1 }),
        run({ id: 7, state: "interactive", worker_id: 2 }),
        run({ id: 6, state: "queued", queued_at: "2026-10-05T07:05:00Z" }),
        run({ id: 5, state: "queued", queued_at: "2026-10-05T07:01:00Z" }),
        run({ id: 4, state: "review", step_key: "3" }),
      ],
      total: 6,
      counts: { ...ZERO, running: 1, leased: 1, interactive: 1, queued: 2, review: 1, done: 5, failed: 2, lost: 1 },
      limit: 200,
      offset: 0,
    };
    const summary = summarizeRuns(list);
    expect(summary).toMatchObject({ held: 3, holdingWorkers: 2, queued: 2, review: 1, failed: 2, lost: 1, done: 5, total: 14 });
    expect(summary.oldestQueuedAt).toBe("2026-10-05T07:01:00Z");
    expect(summary.reviewRun?.step_key).toBe("3");
  });
});

describe("runTiming", () => {
  const now = Date.parse("2026-10-05T07:30:00Z");

  it("times a queued run from when it was queued, and only in the browser", () => {
    expect(runTiming(run(), now)).toEqual({ startedAt: null, durationMs: 30 * 60_000, waiting: true });
    expect(runTiming(run(), null).durationMs).toBeNull();
  });

  it("times a held run from its start until now, and an ended one until it ended", () => {
    const leased = run({ state: "running", leased_at: "2026-10-05T07:10:00Z", started_at: "2026-10-05T07:12:00Z" });
    expect(runTiming(leased, now)).toEqual({ startedAt: "2026-10-05T07:12:00Z", durationMs: 18 * 60_000, waiting: false });
    const done = run({ state: "done", leased_at: "2026-10-05T07:10:00Z", finished_at: "2026-10-05T07:25:00Z" });
    expect(runTiming(done, null).durationMs).toBe(15 * 60_000);
    expect(runTiming(run({ state: "cancelled", finished_at: "2026-10-05T07:25:00Z" }), now).durationMs).toBeNull();
  });

  it("splits a duration into hours and minutes", () => {
    expect(durationParts(59_000)).toEqual({ hours: 0, minutes: 0 });
    expect(durationParts(65 * 60_000)).toEqual({ hours: 1, minutes: 5 });
  });
});

describe("dispatch steps", () => {
  const steps = [
    step("1", { status: "done", ready: false, reason: "its status is done, not pending" }),
    step("2"),
    step("3", { ready: false, reason: "it waits for step 2 (pending)" }),
    step("4", { repo: "web" }),
    step("5", { status: "in_progress", ready: false, reason: "its status is in_progress, not pending" }),
  ];

  it("lists the pending steps and folds the others away", () => {
    const { open, settled } = splitSteps(steps);
    expect(open.map((item) => item.key)).toEqual(["2", "3", "4"]);
    expect(settled.map((item) => item.key)).toEqual(["1", "5"]);
  });

  it("keeps only the picked steps that are still ready", () => {
    expect(readySelection(["4", "3", "2", "9"], steps)).toEqual(["4", "2"]);
  });

  it("names each repo the picked steps run in once", () => {
    expect(selectedRepos(["2", "4"], [...steps, step("6")])).toEqual(["api", "web"]);
  });
});

describe("workers for a dispatch", () => {
  const request = { project: "demo", runtime: "any" as const, repos: ["api"] };

  it("reads a runtime as the hub does", () => {
    expect(runtimeAvailable({ available: true })).toBe(true);
    expect(runtimeAvailable({ version: "1.0" })).toBe(true);
    expect(runtimeAvailable({ available: false })).toBe(false);
    expect(runtimeAvailable("2.1.0")).toBe(true);
    expect(runtimeAvailable("")).toBe(false);
    expect(runtimeAvailable(undefined)).toBe(false);
    expect(workerRuntimes(worker())).toEqual(["claude-code"]);
  });

  it("offers only the visitor's own workers that serve the project and are not revoked", () => {
    const workers = [
      worker({ id: 1, name: "b" }),
      worker({ id: 2, name: "a" }),
      worker({ id: 3, owner: "someone-else" }),
      worker({ id: 4, projects: ["other"] }),
      worker({ id: 5, status: "revoked" }),
    ];
    expect(dispatchWorkers(workers, "octo", "demo").map((item) => item.id)).toEqual([2, 1]);
  });

  it("says whether a worker can take the runs now, later, or not as it is", () => {
    expect(fitWorker(worker(), request)).toMatchObject({ fit: "now", free: 2, problem: null });
    expect(fitWorker(worker({ held_runs: 2 }), request)).toMatchObject({ fit: "busy", free: 0 });
    expect(fitWorker(worker({ status: "offline" }), request).fit).toBe("offline");
    expect(fitWorker(worker({ status: "draining", drained_at: "2026-10-05T07:00:00Z" }), request).problem).toEqual({ kind: "draining" });
    expect(fitWorker(worker(), { ...request, runtime: "codex" }).problem).toEqual({ kind: "runtime", runtime: "codex" });
    expect(fitWorker(worker({ runtimes: {} }), request).problem).toEqual({ kind: "runtime", runtime: "any" });
    expect(fitWorker(worker(), { ...request, repos: ["api", "web"] }).problem).toEqual({ kind: "checkout", repos: ["web"] });
    expect(fitWorker(worker(), { ...request, repos: ["web", "api", "docs"] }).problem).toEqual({ kind: "checkout", repos: ["web", "docs"] });
  });

  it("counts the workers that report a runtime", () => {
    const workers = [worker(), worker({ id: 2, runtimes: { opencode: true } }), worker({ id: 3, runtimes: {} })];
    expect(runtimeCount(workers, "any")).toBe(2);
    expect(runtimeCount(workers, "claude-code")).toBe(1);
    expect(runtimeCount(workers, "codex")).toBe(0);
  });

  it("tells what will happen to the runs", () => {
    const free = worker({ id: 1, name: "free" });
    const busy = worker({ id: 2, name: "busy", held_runs: 2 });
    const bare = worker({ id: 3, name: "bare", checkouts: {} });
    expect(dispatchOutlook(0, [free], request, null)).toEqual({ kind: "nothing" });
    expect(dispatchOutlook(1, [], request, null)).toEqual({ kind: "noWorker" });
    expect(dispatchOutlook(2, [busy, free], request, null)).toMatchObject({ kind: "now", fits: [{ worker: { id: 1 } }] });
    expect(dispatchOutlook(1, [busy, bare], request, null)).toMatchObject({ kind: "later", fits: [{ worker: { id: 2 } }] });
    expect(dispatchOutlook(1, [bare], request, null)).toMatchObject({ kind: "none", fits: [{ problem: { kind: "checkout" } }] });
    expect(dispatchOutlook(1, [free, busy], request, 2)).toMatchObject({ kind: "pinned", fit: { fit: "busy" } });
    expect(dispatchOutlook(1, [free], request, 99)).toEqual({ kind: "noWorker" });
  });
});

describe("models", () => {
  const reporting = (models: unknown, available = true) => worker({ runtimes: { opencode: { available, version: "1.18", models } } });

  it("suggests the models the visitor's workers list for the runtime picked, each once, sorted", () => {
    const workers = [reporting(["openai/gpt-5", "anthropic/claude-x"]), reporting(["openai/gpt-5", "", 3]), reporting(null)];
    expect(modelSuggestions(workers, "opencode")).toEqual(["anthropic/claude-x", "openai/gpt-5"]);
    expect(modelSuggestions(workers, "claude-code")).toEqual([]);
    expect(modelSuggestions([reporting(["x/y"], false)], "opencode")).toEqual([]);
  });

  it("suggests nothing for any runtime: a model goes with the runtime that names it", () => {
    expect(modelSuggestions([reporting(["openai/gpt-5"])], "any")).toEqual([]);
  });

  it("takes a model as one trimmed line of at most 200 characters, with a runtime picked", () => {
    expect(readModel("   ", "any")).toEqual({ model: null, problem: null });
    expect(readModel(" sonnet ", "claude-code")).toEqual({ model: "sonnet", problem: null });
    expect(readModel("sonnet", "any").problem).toBe("runtime");
    expect(readModel("a\tb", "codex").problem).toBe("line");
    expect(readModel("m".repeat(200), "codex").problem).toBeNull();
    expect(readModel("m".repeat(201), "codex").problem).toBe("long");
    expect(readModel("\u{1F600}".repeat(200), "codex").problem).toBeNull(); // characters, as the API counts them
  });
});

describe("plan runs", () => {
  const body = {
    id: "rollout",
    repos: [
      { repo: "api", branch: "feat/rollout" },
      { repo: "web", branch: "main" },
      { repo: "docs" },
    ],
    steps: [
      { id: 1, title: "Model", repo: "api", status: "done" },
      { id: 2, title: "Queue", repo: "api", status: "in_progress" },
      { id: 3, title: "Checkpoint: deploy to staging", repo: "web", status: "pending" },
      { id: 4, title: "Page", repo: "web", status: "blocked" },
      { id: 5, title: "Docs", repo: "docs" },
      { id: 6, title: "Notes", what: "Checkpoint. The owner reads the notes.", repo: "api" },
      { id: 7, title: "Release", what: "Write the checkpoint notes", repo: "api", checkpoint: true },
    ],
  };
  const plan = parsePlan(body);
  const readiness = [
    step("1", { status: "done", ready: false }),
    step("2", { status: "in_progress", ready: false }),
    step("3", { repo: "web" }),
    step("4", { repo: "web", status: "blocked", ready: false }),
    step("5", { repo: "docs", ready: false }),
    step("6"),
    step("7"),
  ];

  it("finds checkpoint steps by flag, title, or the first word of what to do", () => {
    expect(plan.steps.filter(isCheckpoint).map((item) => item.key)).toEqual(["3", "6", "7"]);
  });

  it("does every step not done, in the repos those steps name, on the plan's branches", () => {
    const scope = planRunScope(readiness, plan, { api: "feat/rollout", web: "develop" });
    expect(scope.steps.map((item) => item.key)).toEqual(["2", "3", "4", "5", "6", "7"]);
    expect(scope).toMatchObject({ pending: 4, inProgress: 1, blocked: 1, unplaced: [] });
    expect(scope.repos).toEqual([
      { repo: "api", branch: "feat/rollout", defaultBranch: true },
      { repo: "web", branch: "main", defaultBranch: true },
      { repo: "docs", branch: null, defaultBranch: false },
    ]);
    expect(scope.checkpoints.map((item) => item.key)).toEqual(["3", "6", "7"]);
  });

  it("names the steps that have no repo when the plan does not list exactly one", () => {
    const scope = planRunScope([step("2", { repo: null }), step("3", { status: "done", repo: null })], plan);
    expect(scope.unplaced).toEqual(["2"]);
    expect(scope.repos).toEqual([]);
  });

  it("locks Run plan while the plan has an active run, or nothing pending", () => {
    const runs = [
      run({ id: 3, plan_id: "other", kind: "plan", state: "running" }),
      run({ id: 5, step_key: "4", state: "review" }),
      run({ id: 4, step_key: "2", state: "queued" }),
      run({ id: 2, kind: "plan", state: "done" }),
    ];
    expect(planRunLock(runs, "rollout", 3)).toEqual({ kind: "stepRun", id: 4, state: "queued", step: "2" });
    expect(planRunLock([run({ id: 9, kind: "plan", step_key: null, state: "waiting" }), ...runs], "rollout", 3)).toEqual({
      kind: "planRun",
      id: 9,
      state: "waiting",
    });
    expect(planRunLock(runs.slice(3), "rollout", 0)).toEqual({ kind: "noPending" });
    expect(planRunLock(runs.slice(3), "rollout", 1)).toBeNull();
  });

  it("finds the plan's active plan run, and names its phase", () => {
    const runs = [run({ id: 2, kind: "plan", state: "done" }), run({ id: 3, kind: "plan", state: "parked" })];
    expect(activePlanRun(runs, "rollout")?.id).toBe(3);
    expect(activePlanRun(runs, "other")).toBeNull();
    expect(["queued", "leased", "running", "interactive", "verifying", "waiting", "parked"].map((state) => planRunPhase(state as Run["state"]))).toEqual([
      "queued",
      "running",
      "running",
      "running",
      "running",
      "waiting",
      "parked",
    ]);
  });
});
