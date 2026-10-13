import { describe, expect, it } from "vitest";

import { parsePlan } from "@/lib/plans";

import { planCriteria, stepCriteria } from "./criteria";

describe("plan and step criteria", () => {
  const acceptance = [
    { id: "a4", criterion: "Clicking a step opens the inspector.\n", verify: "pnpm test" },
    { id: "a5", what: "No sideways scroll." },
    { id: "a6" },
    "a loose sentence",
  ];

  it("reads the plan's criteria by id", () => {
    expect([...planCriteria(acceptance)]).toEqual([
      ["a4", "Clicking a step opens the inspector."],
      ["a5", "No sideways scroll."],
    ]);
    expect(planCriteria(null).size).toBe(0);
  });

  it("gives a step's ids their text, keeps its own sentences, and marks an id the plan lacks", () => {
    const [step] = parsePlan({ id: "p", steps: [{ id: 3, acceptance: ["a4", "a9", "The board still loads first."] }] }).steps;
    expect(stepCriteria(step, planCriteria(acceptance))).toEqual([
      { id: "a4", text: "Clicking a step opens the inspector." },
      { id: "a9", text: null },
      { id: null, text: "The board still loads first." },
    ]);
  });

  it("has nothing for a prescribed step", () => {
    const [step] = parsePlan({ id: "p", steps: [{ id: 1, what: "Do it." }] }).steps;
    expect(stepCriteria(step, planCriteria(acceptance))).toEqual([]);
  });
});
