import { describe, expect, it } from "vitest";

import type { Worker } from "@/components/workers/queries";

import {
  canRerun,
  decisionOfRun,
  decisionsForYou,
  hasWork,
  HOME_IDLE_MS,
  HOME_LIVE_MS,
  homeRefreshInterval,
  lastFailed,
  myWorkers,
  oldestQueued,
  type Overview,
  type OverviewDecision,
  type OverviewRun,
  ranFor,
  runningRuns,
} from "./model";

function run(id: number, state: OverviewRun["state"], extra: Partial<OverviewRun> = {}): OverviewRun {
  return {
    id,
    kind: "step",
    project: "demo",
    plan_id: "rollout",
    plan_title: "Worker fleet rollout",
    step_key: "2",
    title: "Queue with dispatch and claim",
    state,
    dispatched_by: "octo",
    worker_id: 1,
    worker: "mini",
    runtime: "claude-code",
    model: null,
    steps_total: null,
    steps_done: null,
    run_seconds: 0,
    queued_at: "2026-10-07T01:00:00Z",
    started_at: "2026-10-07T01:01:00Z",
    finished_at: null,
    error: null,
    ...extra,
  };
}

function decision(id: number, yours: boolean, extra: Partial<OverviewDecision> = {}): OverviewDecision {
  return {
    id,
    project: "demo",
    run_id: 12,
    run_state: "waiting",
    plan_id: "fleet",
    plan_title: "Plan runs on the web",
    step_key: "3",
    category: "deploy",
    question: "Deploy to staging now?",
    owner: yours ? "octo" : "hubot",
    yours,
    asked_at: "2026-10-07T01:00:00Z",
    parks_at: "2026-10-08T01:00:00Z",
    ...extra,
  };
}

const QUIET: Overview = {
  counts: { waiting_on_you: 0, running: 0, queued: 0, done_7d: 0, failed_7d: 0, lost_7d: 0 },
  done_by_day: [],
  active_runs: [],
  recent_runs: [],
  open_decisions: [],
  author_waiting: [],
  projects: [],
};

describe("Home's refresh", () => {
  it("asks every 5 seconds while a run is in flight or a decision is open, every 30 otherwise", () => {
    expect(homeRefreshInterval({ state: {} })).toBe(HOME_IDLE_MS);
    expect(homeRefreshInterval({ state: { data: QUIET } })).toBe(HOME_IDLE_MS);
    expect(homeRefreshInterval({ state: { data: { ...QUIET, active_runs: [run(1, "queued")] } } })).toBe(HOME_LIVE_MS);
    expect(homeRefreshInterval({ state: { data: { ...QUIET, open_decisions: [decision(7, false)] } } })).toBe(HOME_LIVE_MS);
    expect(hasWork({ ...QUIET, counts: { ...QUIET.counts, running: 1 } })).toBe(true);
    // Runs that ended lately are no reason to hurry.
    const ended: Overview = { ...QUIET, recent_runs: [run(3, "done")], counts: { ...QUIET.counts, done_7d: 4 } };
    expect(hasWork(ended)).toBe(false);
  });
});

describe("what Home names", () => {
  it("lists only the decisions the visitor may answer in Needs you, in the API's order", () => {
    const overview = { open_decisions: [decision(7, true), decision(9, true), decision(8, false)] };
    expect(decisionsForYou(overview).map((item) => item.id)).toEqual([7, 9]);
    expect(decisionOfRun(overview, { id: 12, project: "demo" })?.id).toBe(7);
    expect(decisionOfRun(overview, { id: 12, project: "other" })).toBeNull();
  });

  it("finds the runs at work, the queued run that waited longest and the last failure", () => {
    const active = [run(5, "running"), run(4, "verifying"), run(3, "waiting"), run(2, "queued"), run(1, "queued")];
    expect(runningRuns({ active_runs: active }).map((item) => item.id)).toEqual([5, 4]);
    expect(oldestQueued({ active_runs: active })?.id).toBe(1);
    expect(oldestQueued({ active_runs: [run(5, "running")] })).toBeNull();
    const recent = [run(9, "done"), run(8, "failed", { error: "no result file" }), run(7, "failed")];
    expect(lastFailed({ recent_runs: recent })?.id).toBe(8);
    expect(lastFailed({ recent_runs: [run(9, "done")] })).toBeNull();
  });

  it("says how long a run ran, ticking while it runs", () => {
    expect(ranFor(run(1, "running"), Date.parse("2026-10-07T01:13:00Z"))).toBe(12 * 60_000);
    expect(ranFor(run(1, "running"), null)).toBeNull();
    expect(ranFor(run(1, "done", { finished_at: "2026-10-07T01:09:12Z" }), null)).toBe(8 * 60_000 + 12_000);
    expect(ranFor(run(1, "queued", { started_at: null }), Date.now())).toBeNull();
  });
});

describe("Rerun on Home", () => {
  const projects = [
    { name: "demo", role: "writer" },
    { name: "docs", role: "reader" },
  ];

  it("is offered on the visitor's own failed or lost run of one step, where they write", () => {
    expect(canRerun(run(1, "failed"), "octo", projects)).toBe(true);
    expect(canRerun(run(1, "lost"), "octo", projects)).toBe(true);
    expect(canRerun(run(1, "failed"), "octo", [{ name: "demo", role: "admin" }])).toBe(true);
  });

  it("is not offered on someone else's run, a reader's, a plan run, or a run that ended well or was cancelled", () => {
    expect(canRerun(run(1, "failed"), "hubot", projects)).toBe(false);
    expect(canRerun(run(1, "failed"), null, projects)).toBe(false);
    expect(canRerun(run(1, "failed", { project: "docs" }), "octo", projects)).toBe(false);
    expect(canRerun(run(1, "failed", { kind: "plan", step_key: null }), "octo", projects)).toBe(false);
    expect(canRerun(run(1, "done"), "octo", projects)).toBe(false);
    expect(canRerun(run(1, "cancelled"), "octo", projects)).toBe(false);
  });
});

describe("the Fleet card", () => {
  const worker = (id: number, name: string, owner: string, status: Worker["status"], held = 0) =>
    ({ id, name, owner, status, held_runs: held }) as Worker;

  it("shows the visitor's own workers that are not revoked, busy first, then by name", () => {
    const workers = [
      worker(1, "zeta", "octo", "online"),
      worker(2, "alpha", "octo", "offline"),
      worker(3, "mini", "octo", "online", 1),
      worker(4, "beta", "octo", "online"),
      worker(5, "other", "hubot", "online", 1),
      worker(6, "gone", "octo", "revoked"),
    ];
    expect(myWorkers(workers, "octo").map((item) => item.name)).toEqual(["mini", "beta", "zeta", "alpha"]);
    expect(myWorkers(workers, null)).toEqual([]);
  });
});
