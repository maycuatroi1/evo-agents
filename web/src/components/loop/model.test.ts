import { describe, expect, it } from "vitest";

import type { BaselineItem, BoardGoal, BoardItem, GoalMetric, LoopGoal, LoopItem } from "@/lib/loop-queries";

import { backlogGroups, backlogOpen, byState, chainsOf, goalWord, idsByCanvas, isProposal, loopSearch, NO_LOOP_FILTERS, openCount, progressOf, proposalGroups, readLoopFilters, subjectOf, totalCount } from "./model";

function item(id: number, values: Partial<LoopItem> = {}): LoopItem {
  return {
    id,
    kind: "build",
    cycle: "development",
    state: "queued",
    reason: null,
    waiting: {},
    attempts: 0,
    failures: 0,
    owner: null,
    goal: "G-1",
    claim: null,
    use_case: null,
    task: null,
    plan: null,
    run: null,
    parent: null,
    root: id,
    hidden: [],
    created_at: "2026-10-20T08:00:00Z",
    updated_at: "2026-10-20T08:00:00Z",
    finished_at: null,
    ...values,
  };
}

const METRIC: GoalMetric = { key: "checkout.conversion", aggregate: "mean", window_days: 30, comparator: ">=", target: 0.6, tenant: null };

function figure(value: number | null, reached: boolean | null): LoopGoal {
  return { code: "G-1", value, reached, items: {} };
}

describe("the Loop page's filters", () => {
  it("reads what it knows from the address and leaves out the rest", () => {
    const params = new URLSearchParams("state=waiting_human&kind=deliver&goal=G-2&view=chain");
    expect(readLoopFilters(params)).toEqual({ state: "waiting_human", kind: "deliver", goal: "G-2", open: false, view: "chain" });
    expect(readLoopFilters(new URLSearchParams("state=asleep&kind=x&goal=G-0&view=grid"))).toEqual(NO_LOOP_FILTERS);
    expect(readLoopFilters(new URLSearchParams("goal=weekly-reorders")).goal).toBe("weekly-reorders");
  });

  it("writes them back, the default view and every empty filter left out", () => {
    expect(loopSearch(NO_LOOP_FILTERS)).toBe("");
    expect(loopSearch({ ...NO_LOOP_FILTERS, state: "queued", goal: "G-1", view: "chain" })).toBe("state=queued&goal=G-1&view=chain");
  });
});

describe("work items", () => {
  it("list by state, agents at work and people needed first, the newest change first within a state", () => {
    const listed = byState([
      item(1, { state: "done", updated_at: "2026-10-20T09:00:00Z" }),
      item(2, { state: "queued", updated_at: "2026-10-20T07:00:00Z" }),
      item(3, { state: "waiting_human" }),
      item(4, { state: "queued", updated_at: "2026-10-20T10:00:00Z" }),
      item(5, { state: "running" }),
    ]);
    expect(listed.map((found) => found.id)).toEqual([5, 3, 4, 2, 1]);
  });

  it("group by chain, each after the one it came of, the chain changed last first", () => {
    const chains = chainsOf([
      item(1, { kind: "use_case", root: 1, updated_at: "2026-10-19T08:00:00Z" }),
      item(2, { parent: 1, root: 1 }),
      item(3, { kind: "deliver", parent: 2, root: 1, updated_at: "2026-10-20T12:00:00Z" }),
      item(4, { parent: 1, root: 1 }),
      item(5, { kind: "brainstorm", root: 5, updated_at: "2026-10-20T09:00:00Z" }),
      item(7, { kind: "deliver", parent: 6, root: 6 }),
    ]);
    expect(chains.map((chain) => chain.root)).toEqual([1, 5, 6]);
    expect(chains[0].rows.map((row) => [row.item.id, row.depth])).toEqual([
      [1, 0],
      [2, 1],
      [3, 2],
      [4, 1],
    ]);
    expect(chains[2].rows.map((row) => [row.item.id, row.depth])).toEqual([[7, 0]]);
  });

  it("are named by what they are about, or by their kind alone", () => {
    expect(subjectOf(item(1, { kind: "brainstorm", goal: "G-3" }))).toBe("G-3");
    expect(subjectOf(item(1, { kind: "discover", claim: { canvas: "northwind", code: "CL-4" } }))).toBe("CL-4");
    expect(subjectOf(item(1, { kind: "discover", goal: "G-2" }))).toBe("G-2");
    expect(subjectOf(item(1, { kind: "act", claim: { canvas: "northwind", code: "CL-6" } }))).toBe("CL-6");
    expect(subjectOf(item(1, { kind: "use_case", claim: { canvas: "northwind", code: "CL-2" } }))).toBe("CL-2");
    expect(subjectOf(item(1, { kind: "build", use_case: { document: "demo-shop", code: "UC ORD-1", version: 1 } }))).toBe("UC ORD-1");
    expect(subjectOf(item(1, { kind: "deliver", plan: "northwind-one-tap" }))).toBe("northwind-one-tap");
    expect(subjectOf(item(1, { kind: "deliver" }))).toBeNull();
  });

  it("count what the Loop still does, a state the hub left out counting none", () => {
    const counts = { queued: 2, running: 1, waiting_human: 1, done: 4 };
    expect(openCount(counts)).toBe(4);
    expect(totalCount(counts)).toBe(8);
  });
});

describe("goals", () => {
  it("say a closed goal by why it closed", () => {
    expect(goalWord({ status: "active", close_reason: null })).toBe("active");
    expect(goalWord({ status: "closed", close_reason: "achieved" })).toBe("achieved");
    expect(goalWord({ status: "closed", close_reason: null })).toBe("dropped");
  });

  it("measure how near their value is to the target, whichever way the target points", () => {
    expect(progressOf(METRIC, figure(0.45, false))).toBeCloseTo(75);
    expect(progressOf(METRIC, figure(0.7, true))).toBe(100);
    expect(progressOf(METRIC, figure(null, null))).toBeNull();
    expect(progressOf(null, figure(0.45, false))).toBeNull();
    const lower: GoalMetric = { ...METRIC, comparator: "<=", target: 0.05 };
    expect(progressOf(lower, figure(0.1, false))).toBeCloseTo(50);
    expect(progressOf(lower, figure(0, true))).toBe(100);
  });
});

function useCase(code: string, heading: string | null, state: BaselineItem["state"]): BaselineItem {
  return { code, title: code, heading, state, source: "proposal", reason: "", evidence: [] };
}

describe("backlogGroups", () => {
  const items = [useCase("UC-1", "Cart", "undelivered"), useCase("UC-2", null, "unknown"), useCase("UC-3", "Cart", "delivered"), useCase("UC-4", "Pay", "undelivered")];

  it("groups by heading in order of appearance and puts the use cases without one last", () => {
    const groups = backlogGroups(items);
    expect(groups.map((group) => group.heading)).toEqual(["Cart", "Pay", null]);
    expect(groups[0]?.counts).toEqual({ undelivered: 1, delivering: 0, unknown: 0, delivered: 1 });
  });

  it("counts the backlog as the undelivered use cases", () => {
    expect(backlogOpen(items)).toBe(2);
    expect(backlogGroups([])).toEqual([]);
  });
});

const G1: BoardGoal = { code: "G-1", statement: "Shops reorder every week", metric: "reorder last over 90 days >= 0.5" };
const G10: BoardGoal = { code: "G-10", statement: "Shops find a product in two taps", metric: "no metric" };

function proposed(id: number, run: number | null, goal: BoardGoal | null, values: { canvas?: string; reason?: string } = {}): BoardItem {
  return {
    group: "canvas",
    kind: "claim_proposal",
    title: `Proposal #${id}`,
    since: `2026-10-28T09:${String(id).padStart(2, "0")}:00Z`,
    decided_by: "writer",
    deciders: ["chi"],
    link: `/p/northwind/x/canvas/canvases/pantry#proposal-${id}`,
    refs: { proposal: id },
    proposal: {
      id,
      canvas: values.canvas ?? "pantry",
      kind: "create",
      claim: null,
      cell: "solution",
      text: `Claim ${id}`,
      fields: ["cell", "text"],
      status: null,
      test: null,
      reason: values.reason ?? "Moves the metric",
      synthetic: false,
      run,
      goal,
    },
  };
}

describe("proposalGroups", () => {
  const baseline: BoardItem = { ...proposed(1, null, null), kind: "baseline", proposal: null };
  const items = [baseline, proposed(12, 1094, G10), proposed(5, 1097, G1), proposed(3, 1097, G1), proposed(7, 1098, G1, { reason: "Another" }), proposed(9, null, null), proposed(8, 1095, null)];

  it("groups by goal, G-1 before G-10 and no goal last, then by run, the newest first", () => {
    const groups = proposalGroups(items);
    expect(groups.map((group) => [group.key, group.count])).toEqual([
      ["G-1", 3],
      ["G-10", 1],
      ["none", 2],
    ]);
    expect(groups[0]?.runs.map((run) => [run.run, run.items.map((item) => item.proposal.id)])).toEqual([
      [1098, [7]],
      [1097, [3, 5]],
    ]);
    expect(groups[2]?.runs.map((run) => run.run)).toEqual([1095, null]);
  });

  it("says a run's reason once when all its proposals give it", () => {
    const [first] = proposalGroups(items);
    expect(first?.runs[1]?.reason).toBe("Moves the metric");
    expect(first?.runs[0]?.reason).toBeNull();
    expect(first?.runs[1]?.since).toBe("2026-10-28T09:03:00Z");
  });

  it("keeps the proposals apart from the baselines and splits a rejection by canvas", () => {
    expect(items.filter(isProposal)).toHaveLength(6);
    const mixed = [proposed(1, 2, G1), proposed(2, 2, G1, { canvas: "other" }), proposed(3, 2, G1)].filter(isProposal);
    expect([...idsByCanvas(mixed)]).toEqual([
      ["pantry", [1, 3]],
      ["other", [2]],
    ]);
  });
});
