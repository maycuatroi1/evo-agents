import { describe, expect, it } from "vitest";

import { problemsOf, type ProjectProblem } from "@/lib/problem-queries";
import { nextProblemCode, problemNumber, reopening } from "@/lib/problem-writes";
import type { Signal } from "@/lib/signal-queries";

import { aggregateOf, byStatusThenRank, EMPTY_PROBLEM, formOf, formProblems, metricFilter, metricPoints, readStatusFilter, statusCounts, writeOf } from "./problem-model";

function problem(number: number, values: Partial<ProjectProblem> = {}): ProjectProblem {
  return {
    project: "northwind",
    code: `P-${number}`,
    number,
    revision: 1,
    statement: "Shop owners lose a sale when checkout takes more than two minutes",
    who: "Owners of small shops",
    when: "At lunch, when queues grow",
    workaround: "They take the order on paper",
    metric: null,
    rank: number,
    owner: "mai",
    status: "open",
    close_reason: null,
    note: null,
    created_by: "mai",
    created_at: "2026-10-20T08:00:00Z",
    ...values,
  };
}

const NOW = Date.parse("2026-10-24T12:00:30Z");
const DAY = 24 * 3600 * 1000;

function signal(id: number, value: number | null, daysAgo: number, tenant: string | null = "lumen-shops"): Signal {
  return {
    id,
    key: "checkout.conversion",
    kind: "number",
    value,
    text: null,
    source: "lumen-analytics",
    measured_at: new Date(NOW - daysAgo * DAY).toISOString(),
    tenant,
    synthetic: false,
    external_id: null,
    label: {},
    via: "member",
    ingest_key: null,
    login: "mai",
    run_id: null,
    batch_id: "b1",
    received_at: new Date(NOW).toISOString(),
  };
}

describe("the problems list", () => {
  it("lists the open problems first, then the solved and the dropped ones, by rank within each", () => {
    const listed = [problem(1, { status: "dropped", rank: 1 }), problem(2, { rank: 3 }), problem(3, { status: "solved", rank: 2 }), problem(4, { rank: 1 })];
    expect(byStatusThenRank(listed).map((found) => found.code)).toEqual(["P-4", "P-2", "P-3", "P-1"]);
    expect(statusCounts(listed)).toEqual({ open: 2, solved: 1, dropped: 1 });
  });

  it("reads its status filter from the address, a status it does not know as every status", () => {
    expect(readStatusFilter(new URLSearchParams("status=solved"))).toBe("solved");
    expect(readStatusFilter(new URLSearchParams("status=closed"))).toBeNull();
    expect(readStatusFilter(new URLSearchParams(""))).toBeNull();
  });

  it("gives a new problem the code after the highest the project ever gave", () => {
    expect(nextProblemCode([])).toBe("P-1");
    expect(nextProblemCode([problem(1), problem(7, { status: "dropped" }), problem(3)])).toBe("P-8");
    expect(problemNumber("P-12")).toBe(12);
    expect(problemNumber("P-0")).toBeNull();
  });

  it("reads the problems a plan's body names, each once, and none from a body without", () => {
    expect(problemsOf({ problems: ["P-2", "P-1", "P-2", "nope", 3] })).toEqual(["P-2", "P-1"]);
    expect(problemsOf({ title: "x" })).toEqual([]);
    expect(problemsOf(null)).toEqual([]);
  });
});

describe("a problem's metric", () => {
  const metric = { key: "checkout.conversion", aggregate: "mean" as const, window_days: 30, tenant: "lumen-shops" };

  it("reads its key and tenant over its window, from the minute", () => {
    expect(metricFilter(metric, NOW)).toEqual({ key: "checkout.conversion", tenant: "lumen-shops", since: "2026-09-24T12:00:00.000Z", limit: 1000 });
    expect(metricFilter({ ...metric, window_days: null, tenant: null }, NOW).since).toBe("2026-07-26T12:00:00.000Z");
  });

  it("keeps the values of its window and tenant, oldest first", () => {
    const signals = [signal(3, 0.5, 1), signal(1, 0.42, 3), signal(2, null, 2), signal(4, 0.45, 2), signal(5, 0.9, 40), signal(6, 0.1, 1, "other")];
    expect(metricPoints(signals, metric, NOW).map((point) => point.value)).toEqual([0.42, 0.45, 0.5]);
    expect(metricPoints(signals, { ...metric, tenant: null }, NOW).map((point) => point.value)).toEqual([0.42, 0.45, 0.5, 0.1]);
  });

  it("sums the values up as the hub sums a goal's metric, the percentiles by the nearest rank", () => {
    const points = [0.42, 0.45, 0.5, 0.2].map((value, index) => ({ id: index, at: index, time: "", value }));
    expect(aggregateOf(points, "last")).toBe(0.2);
    expect(aggregateOf(points, "mean")).toBeCloseTo(0.3925);
    expect(aggregateOf(points, "sum")).toBeCloseTo(1.57);
    expect(aggregateOf(points, "count")).toBe(4);
    expect(aggregateOf(points, "p50")).toBe(0.42);
    expect(aggregateOf(points, "p90")).toBe(0.5);
    expect(aggregateOf(points.slice(0, 3), "p50")).toBe(0.45);
    expect(aggregateOf([], "mean")).toBeNull();
    expect(aggregateOf([], "count")).toBeNull();
  });
});

describe("the problem's form", () => {
  it("names each field the hub would refuse", () => {
    expect(formProblems(EMPTY_PROBLEM)).toEqual(["statement", "who", "when", "workaround"]);
    const filled = formOf(problem(1));
    expect(formProblems(filled)).toEqual([]);
    expect(formProblems({ ...filled, rank: "0", owner: "no spaces", metricKey: "checkout.conversion", windowDays: "91" })).toEqual(["windowDays", "rank", "owner"]);
    expect(formProblems({ ...filled, windowDays: "91" })).toEqual([]);
  });

  it("creates a new problem only, and edits one at the revision the page read, keeping its status", () => {
    const form = { ...EMPTY_PROBLEM, statement: " What hurts ", who: "who", when: "when", workaround: "cope", metricKey: "checkout.conversion", windowDays: "30" };
    expect(writeOf(form, null)).toEqual({
      statement: "What hurts",
      who: "who",
      when: "when",
      workaround: "cope",
      metric: { key: "checkout.conversion", aggregate: "mean", window_days: 30, tenant: null },
      rank: null,
      owner: null,
      status: "open",
      close_reason: null,
      note: null,
      if_revision: 0,
    });
    const solved = problem(2, { revision: 4, status: "solved", close_reason: "Checkout takes 20 seconds now" });
    const write = writeOf({ ...formOf(solved), note: "who is hurt, said better" }, solved);
    expect(write).toMatchObject({ status: "solved", close_reason: "Checkout takes 20 seconds now", if_revision: 4, rank: 2, owner: "mai", metric: null, note: "who is hurt, said better" });
  });

  it("reopens a closed problem as it is, without the reason it was closed", () => {
    const dropped = problem(3, { revision: 2, status: "dropped", close_reason: "Not ours to fix" });
    expect(reopening(dropped, "  ")).toMatchObject({ status: "open", close_reason: null, note: null, if_revision: 2, statement: dropped.statement });
  });
});
