import { describe, expect, it } from "vitest";

import { proposalGroups, proposedClaim, testCardFrom } from "./proposal-model";

describe("proposals", () => {
  const agent = (run: number) => ({ actor: "agent" as const, login: "mai", run_id: run, runtime: "claude-code", model: null, chat_message_id: null });
  const proposal = (id: number, reason: string, run = 7) => ({ id, reason, provenance: agent(run) });

  it("gathers proposals one run made for one reason at the first of them, in the hub's order", () => {
    const groups = proposalGroups([proposal(1, "Goal G-2"), proposal(2, "Value check"), proposal(3, "Goal G-2"), proposal(4, "Goal G-2", 8)]);
    expect(groups.map((group) => group.proposals.map((item) => item.id))).toEqual([[1, 3], [2], [4]]);
    expect(groups[0]?.reason).toBe("Goal G-2");
  });

  it("leaves a proposal without a reason on its own", () => {
    expect(proposalGroups([proposal(1, " "), proposal(2, "")]).map((group) => group.proposals.length)).toEqual([1, 1]);
  });

  it("reads a new claim from the fields it sets, its test card whole", () => {
    const claim = proposedClaim({
      cell: "key_metrics",
      text: "Activation predicts retention",
      confidence: "medium",
      signal_keys: ["crm.activated", "crm.retained"],
      test_card: { metric: "crm.retained", threshold: 3, comparator: ">=", deadline: "2027-01-31" },
      position: 2,
      use_cases: [],
    });
    expect(claim).toMatchObject({ cell: "key_metrics", confidence: "medium", signalKeys: ["crm.activated", "crm.retained"], useCases: [], problems: [] });
    expect(claim.testCard).toMatchObject({ metric: "crm.retained", threshold: 3, hypothesis: null, tenant: null, deadline: "2027-01-31" });
    expect(claim.other).toEqual([{ field: "position", value: 2 }]);
  });

  it("knows no cell, confidence or test card it does not recognise", () => {
    expect(proposedClaim({ cell: "elsewhere", confidence: "certain", test_card: {} })).toMatchObject({ cell: null, confidence: null, testCard: null, text: "" });
    expect(testCardFrom(["crm.retained"])).toBeNull();
  });
});
