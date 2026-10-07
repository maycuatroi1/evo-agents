import { describe, expect, it } from "vitest";

import type { Overview, OverviewRun } from "@/components/home/model";
import type { PlanSummary } from "@/lib/plans";

import {
  canRegister,
  dispatchProjects,
  fold,
  grantedProjects,
  initialScope,
  matchesWords,
  orderPlans,
  overviewRuns,
  planRunOffers,
  queryWords,
  rerunCandidate,
  runHaystack,
  runSearchText,
  scopeCycle,
  scopeProjects,
  stepScope,
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
    finished_at: "2026-10-07T01:09:00Z",
    error: null,
    ...extra,
  };
}

function plan(planId: string, area: PlanSummary["area"], done: number, total: number, updated = "2026-10-07T01:00:00Z"): PlanSummary {
  return {
    plan_id: planId,
    area,
    revision: 1,
    digest: "d",
    title: `Plan ${planId}`,
    steps_total: total,
    steps_done: done,
    updated_at: updated,
    updated_by: "octo",
  };
}

const PROJECTS: Overview["projects"] = [
  { name: "demo", role: "writer", max_level: "internal", repos: 1, active_plans: 2, open_decisions: 0 },
  { name: "docs", role: "reader", max_level: "public", repos: 1, active_plans: 1, open_decisions: 0 },
];

const GRANTS = [
  { project: "docs", role: "reader" },
  { project: "demo", role: "writer" },
  { project: "ops", role: "admin" },
];

describe("matching a query", () => {
  it("folds case and Vietnamese diacritics", () => {
    expect(fold("Phát hành evo-agents 0.4.0")).toBe("phat hanh evo-agents 0.4.0");
    expect(fold("Đồng bộ ĐƯỜNG")).toBe("dong bo duong");
    expect(queryWords("  Phát   HÀNH ")).toEqual(["phat", "hanh"]);
  });

  it("keeps what holds every word, anywhere", () => {
    const text = "#12 Phát hành evo-agents 0.4.0 rollout demo";
    expect(matchesWords([], text)).toBe(true);
    expect(matchesWords(queryWords("phat 0.4"), text)).toBe(true);
    expect(matchesWords(queryWords("#12"), text)).toBe(true);
    expect(matchesWords(queryWords("#12 docs"), text)).toBe(false);
  });

  it("trims what the runs list's q receives and keeps it under 200 characters", () => {
    expect(runSearchText("  #12 ")).toBe("#12");
    expect(runSearchText("x".repeat(250))).toHaveLength(200);
    expect(runSearchText("   ")).toBe("");
  });

  it("finds a run by its number, title, plan, step, project, worker and owner", () => {
    const text = runHaystack(run(12, "failed"));
    for (const word of ["#12", "queue", "rollout", "fleet", "demo", "mini", "octo"]) {
      expect(matchesWords(queryWords(word), text), word).toBe(true);
    }
  });
});

describe("scopes", () => {
  it("lists the projects of the grants by name, then every project", () => {
    const projects = grantedProjects([...GRANTS, { project: "demo", role: "reader" }]);
    expect(projects).toEqual(["demo", "docs", "ops"]);
    expect(scopeCycle(projects)).toEqual(["demo", "docs", "ops", null]);
  });

  it("starts in the page's project when the visitor holds a grant on it, else in every project", () => {
    expect(initialScope("docs", ["demo", "docs"])).toBe("docs");
    expect(initialScope("hidden", ["demo", "docs"])).toBeNull();
    expect(initialScope(null, ["demo"])).toBeNull();
  });

  it("moves forward with Tab and back with Shift Tab, wrapping around", () => {
    const cycle = scopeCycle(["demo", "docs"]);
    expect(stepScope(cycle, "demo", 1)).toBe("docs");
    expect(stepScope(cycle, "docs", 1)).toBeNull();
    expect(stepScope(cycle, null, 1)).toBe("demo");
    expect(stepScope(cycle, "demo", -1)).toBeNull();
    expect(stepScope(cycle, null, -1)).toBe("docs");
    expect(stepScope(cycle, "gone", 1)).toBe("demo");
    expect(stepScope([null], null, 1)).toBeNull();
  });

  it("covers the project it names, or every project of the grants", () => {
    expect(scopeProjects("docs", ["demo", "docs"])).toEqual(["docs"]);
    expect(scopeProjects(null, ["demo", "docs"])).toEqual(["demo", "docs"]);
    expect(scopeProjects(null, Array.from({ length: 30 }, (_, index) => `p${index}`))).toHaveLength(20);
  });
});

describe("actions the grants allow", () => {
  it("offers Run plan on active plans with steps left, where the visitor writes", () => {
    const offers = planRunOffers(
      [
        { project: "demo", plans: [plan("fleet", "active", 2, 5), plan("done-active", "active", 4, 4), plan("old", "completed", 3, 3)] },
        { project: "docs", plans: [plan("guides", "active", 0, 2)] },
        { project: "ops", plans: [plan("deploy", "active", 1, 3)] },
      ],
      GRANTS,
    );
    expect(offers).toEqual([
      { project: "demo", planId: "fleet", title: "Plan fleet", left: 3 },
      { project: "ops", planId: "deploy", title: "Plan deploy", left: 2 },
    ]);
  });

  it("dispatches and registers only with the writer or admin role", () => {
    expect(dispatchProjects(["demo", "docs", "ops", "hidden"], GRANTS)).toEqual(["demo", "ops"]);
    expect(canRegister(GRANTS)).toBe(true);
    expect(canRegister([{ project: "docs", role: "reader" }])).toBe(false);
    expect(canRegister([])).toBe(false);
  });
});

describe("the run Rerun names", () => {
  const overview = (recent: OverviewRun[], active: OverviewRun[] = []) => ({ recent_runs: recent, active_runs: active, projects: PROJECTS });

  it("is the visitor's latest failed or lost run of one step, where they write", () => {
    const recent = [run(9, "done", { step_key: "4" }), run(8, "lost"), run(7, "failed", { step_key: "3" })];
    expect(rerunCandidate(overview(recent), "octo", null)?.id).toBe(8);
    expect(rerunCandidate(overview(recent), "octo", "demo")?.id).toBe(8);
    expect(rerunCandidate(overview(recent), "octo", "docs")).toBeNull();
  });

  it("skips someone else's run, a plan run, a run where the visitor reads, and a step run again since", () => {
    expect(rerunCandidate(overview([run(8, "failed", { dispatched_by: "hubot" })]), "octo", null)).toBeNull();
    expect(rerunCandidate(overview([run(8, "failed", { kind: "plan", step_key: null })]), "octo", null)).toBeNull();
    expect(rerunCandidate(overview([run(8, "failed", { project: "docs" })]), "octo", null)).toBeNull();
    expect(rerunCandidate(overview([run(8, "failed")], [run(11, "queued")]), "octo", null)).toBeNull();
    expect(rerunCandidate(overview([run(10, "done"), run(8, "failed")]), "octo", null)).toBeNull();
    expect(rerunCandidate(overview([run(10, "done", { step_key: "4" }), run(8, "failed")]), "octo", null)?.id).toBe(8);
    expect(rerunCandidate(overview([run(8, "failed")]), null, null)).toBeNull();
  });
});

describe("runs and plans as listed", () => {
  it("lists the overview's runs in flight first, then the ones that ended, in the scope", () => {
    const data = {
      active_runs: [run(13, "running"), run(14, "queued", { project: "docs" })],
      recent_runs: [run(9, "done"), run(13, "running")],
    };
    expect(overviewRuns(data, null).map((item) => item.id)).toEqual([13, 14, 9]);
    expect(overviewRuns(data, "docs").map((item) => item.id)).toEqual([14]);
    expect(overviewRuns(undefined, null)).toEqual([]);
  });

  it("puts active plans first, the last changed first within each area", () => {
    const plans = [
      plan("a", "completed", 1, 1, "2026-10-07T03:00:00Z"),
      plan("b", "active", 0, 1, "2026-10-05T00:00:00Z"),
      plan("c", "active", 0, 1, "2026-10-06T00:00:00Z"),
    ];
    expect(orderPlans(plans).map((item) => item.plan_id)).toEqual(["c", "b", "a"]);
  });
});
