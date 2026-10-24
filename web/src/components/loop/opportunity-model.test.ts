import { describe, expect, it } from "vitest";

import type { ProjectOpportunity } from "@/lib/opportunity-queries";

import { bySignal, byClosingWindow, daysLeft, EMPTY_OPPORTUNITY, formOf, formProblems, parseAnswers, readStatusFilter, statusCounts, writeOf } from "./opportunity-model";

function held(values: Partial<ProjectOpportunity> = {}): ProjectOpportunity {
  return {
    project: "northwind",
    code: "O-1",
    number: 1,
    revision: 2,
    statement: "A pilot with the regional chain",
    beneficiary: "Shop owners",
    leverage: "An intro from the fair",
    window_until: "2026-12-01",
    window_note: "the fair",
    answers: ["P-1", "lumen/P-3"],
    hidden_answers: 1,
    metric: null,
    rank: 1,
    owner: "mai",
    status: "open",
    close_reason: null,
    note: null,
    created_by: "mai",
    created_at: "2026-10-20T08:00:00Z",
    ...values,
  };
}

describe("the list", () => {
  it("orders by the nearest closing window, a window already past first, those with no date last", () => {
    const rows = [
      { number: 1, rank: 1, window_until: null },
      { number: 2, rank: 2, window_until: "2026-12-01" },
      { number: 3, rank: 3, window_until: "2026-10-01" },
      { number: 4, rank: 1, window_until: "2026-12-01" },
      { number: 5, rank: 2, window_until: null },
    ];
    expect(byClosingWindow(rows).map((row) => row.number)).toEqual([3, 4, 2, 1, 5]);
  });

  it("counts by status and reads the status filter of the address", () => {
    expect(statusCounts([held(), held({ status: "missed" }), held({ status: "open" })])).toEqual({ open: 2, captured: 0, missed: 1, dropped: 0 });
    expect(readStatusFilter(new URLSearchParams("status=missed"))).toBe("missed");
    expect(readStatusFilter(new URLSearchParams("status=solved"))).toBeNull();
  });

  it("counts the whole days left to a window, negative once past", () => {
    const now = Date.parse("2026-10-25T15:30:00Z");
    expect(daysLeft("2026-11-04", now)).toBe(10);
    expect(daysLeft("2026-10-25", now)).toBe(0);
    expect(daysLeft("2026-10-22", now)).toBe(-3);
    expect(daysLeft(null, now)).toBeNull();
  });
});

describe("the form", () => {
  it("round-trips an opportunity and keeps what the form does not edit", () => {
    const one = held({ status: "missed", close_reason: "the fair closed", metric: { key: "orders", aggregate: "sum", window_days: 7, tenant: null } });
    const write = writeOf(formOf(one), one);
    expect(write).toMatchObject({ statement: one.statement, window_until: "2026-12-01", answers: ["P-1", "lumen/P-3"], rank: 1, status: "missed", close_reason: "the fair closed", if_revision: 2 });
    expect(write.metric).toEqual(one.metric);
  });

  it("creates with if_revision 0 and names the fields the hub would refuse", () => {
    expect(writeOf(EMPTY_OPPORTUNITY, null)).toMatchObject({ if_revision: 0, status: "open", rank: null, owner: null, window_until: null });
    expect(formProblems(EMPTY_OPPORTUNITY)).toEqual(["statement", "beneficiary", "leverage"]);
    const form = { ...formOf(held()), windowUntil: "2026-02-30", answers: "P-1, nope", rank: "0", owner: "-x" };
    expect(formProblems(form)).toEqual(["windowUntil", "answers", "rank", "owner"]);
  });

  it("reads answers apart by commas or spaces, each once", () => {
    expect(parseAnswers("P-1, lumen/P-3  P-1\nP-2")).toEqual(["P-1", "lumen/P-3", "P-2"]);
  });
});

describe("the evidence", () => {
  it("groups the lines by signal in the hub's order, the lines by number", () => {
    const lines = [
      { signal: "wish", number: 3 },
      { signal: "complaint", number: 2 },
      { signal: "wish", number: 1 },
      { signal: "other", number: 4 },
    ];
    expect(bySignal(lines).map((group) => [group.signal, group.lines.map((line) => line.number)])).toEqual([
      ["complaint", [2]],
      ["wish", [1, 3]],
      ["other", [4]],
    ]);
  });
});
