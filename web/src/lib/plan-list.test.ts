import { describe, expect, it } from "vitest";

import { changeKind, firstSentence, groupPlans, readFilters, sortPlans, writeFilters } from "./plan-list";
import type { PlanSummary } from "./plans";

function plan(id: string, fields: Partial<PlanSummary> = {}): PlanSummary {
  return {
    plan_id: id,
    area: "active",
    revision: 1,
    digest: "d",
    title: null,
    steps_total: 4,
    steps_done: 0,
    updated_at: "2026-10-01T00:00:00Z",
    updated_by: "alice",
    ...fields,
  };
}

describe("plan list filters", () => {
  it("reads junk in the address as the defaults and writes only what differs from them", () => {
    const filters = readFilters(new URLSearchParams("view=archived&group=repo&sort=nope&q=%20kg%20&owner=&plan=Not%20a%20plan"));
    expect(filters).toEqual({ view: "open", q: "kg", owner: null, problem: null, repo: null, group: "repo", sort: "updated", plan: null });
    expect(writeFilters(filters)).toBe("?q=kg&group=repo");
    // The plan of the peek panel is kept, so a reload opens it again.
    expect(writeFilters(readFilters(new URLSearchParams("plan=kg-wiring&view=completed")))).toBe("?view=completed&plan=kg-wiring");
    expect(writeFilters(readFilters(new URLSearchParams()))).toBe("");
  });
});

describe("plan names", () => {
  it("takes the goal's first sentence on one line, cut to 120 characters", () => {
    expect(firstSentence("Nối graph\n  vào hub. Rồi đo.")).toBe("Nối graph vào hub.");
    expect(firstSentence("v1.2 ships")).toBe("v1.2 ships");
    expect(firstSentence("x".repeat(200))).toHaveLength(120);
    expect(firstSentence("  ")).toBeNull();
  });
});

describe("plan order and groups", () => {
  const plans = [
    plan("b", { updated_at: "2026-10-02T00:00:00Z", steps_done: 1, problems: ["P-10", "P-2"] }),
    plan("a", { updated_at: "2026-10-03T00:00:00Z", steps_done: 3 }),
    plan("c", { updated_at: "2026-10-01T00:00:00Z", steps_done: 3, problems: ["P-2"] }),
  ];

  it("sorts by last change, then by progress with ties by last change", () => {
    expect(sortPlans(plans, "updated").map((p) => p.plan_id)).toEqual(["a", "b", "c"]);
    expect(sortPlans(plans, "progress").map((p) => p.plan_id)).toEqual(["a", "c", "b"]);
  });

  it("puts a plan under each of its problems, codes by number, and the plans without one last", () => {
    const groups = groupPlans(plans, "problem");
    expect(groups.map((group) => [group.key, group.plans.map((p) => p.plan_id)])).toEqual([
      ["P-2", ["b", "c"]],
      ["P-10", ["b"]],
      [null, ["a"]],
    ]);
  });
});

describe("changeKind", () => {
  it("says the time today, Yesterday, the weekday this week, then the date", () => {
    expect(changeKind("2026-11-26", "2026-11-26")).toBe("time");
    expect(changeKind("2026-11-25", "2026-11-26")).toBe("yesterday");
    expect(changeKind("2026-11-20", "2026-11-26")).toBe("weekday");
    expect(changeKind("2026-11-19", "2026-11-26")).toBe("date");
    expect(changeKind("2025-12-30", "2026-01-02")).toBe("weekday");
    expect(changeKind("2025-11-30", "2026-01-02")).toBe("dateYear");
  });
});
