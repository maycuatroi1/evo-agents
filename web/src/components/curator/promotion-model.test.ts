import { describe, expect, it } from "vitest";

import { promotionView } from "./promotion-model";

describe("promotionView", () => {
  it("reads a promotion into a shared memory, and what accepting it wrote", () => {
    const raw = {
      sources: [
        { memory_id: 7, revision: 2, agent: "reviewer" },
        { memory_id: "x", revision: 1 },
      ],
      target: { memory_id: 3, revision: 4 },
      body: "---\\nname: Deploy\\n---\\nShip on Fridays.",
      label: { level: "internal" },
      written: { memory_id: 3, revision: 5 },
    };
    expect(promotionView(raw)).toEqual({
      sources: [{ memoryId: 7, revision: 2, agent: "reviewer" }],
      target: { kind: "merge", memoryId: 3, revision: 4 },
      body: "---\\nname: Deploy\\n---\\nShip on Fridays.",
      written: { memoryId: 3, revision: 5 },
    });
  });

  it("reads a new shared memory as its target, nothing written yet, and no promotion at all", () => {
    const raw = { sources: [], target: { location: "harness", name: "deploy.md", type: "project" }, body: "x" };
    expect(promotionView(raw)).toMatchObject({ target: { kind: "new", location: "harness", name: "deploy.md", type: "project" }, written: null });
    expect(promotionView({ target: { memory_id: 3 } })).toMatchObject({ target: null, body: "", sources: [] });
    expect(promotionView(null)).toBeNull();
    expect(promotionView(undefined)).toBeNull();
    expect(promotionView([1])).toBeNull();
  });
});
