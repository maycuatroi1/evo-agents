import { describe, expect, it } from "vitest";

import { firstCheckouts, groupCheckouts } from "./checkouts";
import { readCheckouts } from "./model";

describe("the Checkouts card", () => {
  it("groups checkouts by project and names their shared directory once", () => {
    const { base, groups } = groupCheckouts(
      readCheckouts({
        "evo-lms/harness-hub": { path: "/home/evo/github/evo-lms-harness", branch: "master" },
        "evo-agents/evo-agents": { path: "/home/evo/github/evo-agents/", branch: "main" },
        "evo-lms/evo-cli": { path: "/home/evo/github/evo-cli", branch: "main" },
        scratch: { path: "/home/evo/github/scratch" },
      }),
    );
    expect(base).toBe("/home/evo/github");
    expect(groups.map((group) => [group.project, group.rows.map((row) => [row.name, row.repo, row.where])])).toEqual([
      [null, [["scratch", "scratch", null]]],
      ["evo-agents", [["evo-agents/evo-agents", "evo-agents", null]]],
      [
        "evo-lms",
        [
          ["evo-lms/evo-cli", "evo-cli", null],
          ["evo-lms/harness-hub", "harness-hub", "evo-lms-harness"],
        ],
      ],
    ]);
  });

  it("keeps whole paths when they share no directory, or there is one checkout", () => {
    const apart = groupCheckouts(readCheckouts({ api: "~/work/api", web: "~/github/web" }));
    expect(apart.base).toBeNull();
    expect(apart.groups[0].rows.map((row) => row.where)).toEqual(["~/work/api", "~/github/web"]);
    const one = groupCheckouts(readCheckouts({ "evo-agents": { path: "~/github/evo-agents", branch: "main" } }));
    expect([one.base, one.groups[0].rows[0].where]).toEqual([null, "~/github/evo-agents"]);
    expect(groupCheckouts(readCheckouts({ web: ["~/a/web", "~/a/b"], api: "~/a/api" })).base).toBeNull();
  });

  it("shows the first rows across groups, each with its whole count", () => {
    const { groups } = groupCheckouts(readCheckouts({ "a/one": "~/x/one", "a/two": "~/x/two", "b/three": "~/x/three", "c/four": "~/x/four" }));
    expect(firstCheckouts(groups, 3).map((group) => [group.project, group.total, group.rows.length])).toEqual([
      ["a", 2, 2],
      ["b", 1, 1],
    ]);
    expect(firstCheckouts(groups, 10)).toHaveLength(3);
  });
});
