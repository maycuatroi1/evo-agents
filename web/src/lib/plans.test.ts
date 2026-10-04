import { describe, expect, it } from "vitest";

import {
  comparedPair,
  countSteps,
  dependents,
  filterSteps,
  parsePlan,
  parseStep,
  percent,
  type PlanDiffLine,
  previousRevision,
  revisionParam,
  splitRows,
  stepKey,
  stepLabel,
  stepRepos,
} from "./plans";

const body = {
  id: "agent-hub",
  title: "Hub cho agent",
  goal: "Một hub chung cho memories, skills, plans.\n",
  created_at: "2026-10-01T09:00:00+07:00",
  repos: [
    { repo: "evo-agents", branch: "main", order: 1, status: "in_progress", depends_on: [] },
    "evo-agents-harness",
    { branch: "no-repo-key" },
  ],
  steps: [
    { id: 5, title: "API plans", repo: "evo-agents", what: "PUT, PATCH", status: "done", evidence: "abc", done_at: "2026-10-03" },
    { id: "24", title: "Web shell", repo: "evo-agents", what: "Next.js", status: "done", blocking: true },
    { id: 26, title: "Web: plans", repo: "evo-agents", what: "Trang plans", depends_on: [24, "5"], status: "in_progress", why: "đọc" },
    { order: 27, what: "Không có tiêu đề\nvà hai dòng", status: "blocked", blocking: false },
    { title: "Không id", what: "x", status: "skipped" },
    { what: "Không trạng thái", repo: "evo-agents-harness" },
    "một bước chỉ là chuỗi",
  ],
  acceptance: [{ criterion: "c1" }],
  rollback: "revert",
  hub: { project: "p", revision: 1, digest: "sha256:0" },
  risks: null,
};

describe("parsePlan", () => {
  const view = parsePlan(body);

  it("keys steps as evo-cli does: id, else order, else position", () => {
    expect(view.steps.map((step) => step.key)).toEqual(["5", "24", "26", "27", "4", "5", "6"]);
    expect(stepKey({ id: 0 }, 3)).toBe("0");
  });

  it("groups statuses into the four columns, keeping an unknown status as written", () => {
    expect(view.steps.map((step) => step.group)).toEqual([
      "done",
      "done",
      "in_progress",
      "blocked",
      "other",
      "pending",
      "pending",
    ]);
    expect(view.steps[4].rawStatus).toBe("skipped");
    expect(view.steps[5].rawStatus).toBeNull();
  });

  it("reads depends_on as step keys, blocking as a boolean, and keeps the keys it does not lay out", () => {
    expect(view.steps[2].dependsOn).toEqual(["24", "5"]);
    expect(view.steps[1].blocking).toBe(true);
    expect(view.steps[3].blocking).toBe(false);
    expect(view.steps[0].blocking).toBeNull();
    expect(view.steps[2].extra).toEqual([["why", "đọc"]]);
    expect(view.steps[0].evidence).toBe("abc");
    expect(view.steps[0].doneAt).toBe("2026-10-03");
    expect(view.steps[6].what).toBe("một bước chỉ là chuỗi");
  });

  it("keeps the repos it can name and the other sections in order, without the hub key or empty values", () => {
    expect(view.repos.map((repo) => repo.repo)).toEqual(["evo-agents", "evo-agents-harness"]);
    expect(view.repos[0].order).toBe("1");
    expect(view.sections.map(([key]) => key)).toEqual(["acceptance", "rollback"]);
    expect(view.title).toBe("Hub cho agent");
    expect(view.createdAt).toBe("2026-10-01T09:00:00+07:00");
  });

  it("survives a body that is nothing like a plan", () => {
    const odd = parsePlan({ steps: "none", repos: 3 }, "fallback");
    expect(odd).toMatchObject({ id: "fallback", steps: [], repos: [], title: null });
  });
});

describe("steps", () => {
  const steps = parsePlan(body).steps;

  it("counts steps by status", () => {
    expect(countSteps(steps)).toEqual({ pending: 2, in_progress: 1, blocked: 1, done: 2, other: 1, total: 7 });
  });

  it("rounds progress down so only a finished plan shows 100 %", () => {
    expect(percent(13, 13)).toBe(100);
    expect(percent(199, 200)).toBe(99);
    expect(percent(0, 0)).toBe(0);
  });

  it("finds what waits for a step", () => {
    expect(dependents(steps, "24").map((step) => step.key)).toEqual(["26"]);
    expect(dependents(steps, "26")).toEqual([]);
  });

  it("names a step by its title, else the first line of what", () => {
    expect(stepLabel(steps[2])).toBe("Web: plans");
    expect(stepLabel(steps[3])).toBe("Không có tiêu đề");
    expect(stepLabel(parseStep({ title: "x".repeat(200), what: "" }, 0))).toHaveLength(120);
  });

  it("filters by text (ignoring case) and by repo", () => {
    expect(filterSteps(steps, "WEB", "").map((step) => step.key)).toEqual(["24", "26"]);
    expect(filterSteps(steps, "", "evo-agents-harness").map((step) => step.key)).toEqual(["5"]);
    expect(filterSteps(steps, "  ", "")).toHaveLength(7);
    expect(filterSteps(steps, "trang", "evo-agents").map((step) => step.key)).toEqual(["26"]);
    expect(stepRepos(steps)).toEqual(["evo-agents", "evo-agents-harness"]);
  });
});

describe("revisions", () => {
  const revisions = [{ revision: 3 }, { revision: 1 }, { revision: 2 }, { revision: 5 }];

  it("compares the pair asked for, else the latest with the one before", () => {
    expect(comparedPair(revisions, 1, 3)).toEqual({ from: 1, to: 3 });
    expect(comparedPair(revisions, 1, 4)).toEqual({ from: 3, to: 5 });
    expect(comparedPair(revisions, null, null)).toEqual({ from: 3, to: 5 });
    expect(comparedPair([{ revision: 1 }], null, null)).toEqual({ from: 1, to: 1 });
    expect(comparedPair([], 1, 2)).toBeNull();
  });

  it("finds the revision before one, skipping those the reader cannot see", () => {
    expect(previousRevision(revisions, 5)).toBe(3);
    expect(previousRevision(revisions, 1)).toBeNull();
  });

  it("reads a revision from a search parameter only when it is a positive integer", () => {
    expect(revisionParam("12")).toBe(12);
    expect(revisionParam(["4", "5"])).toBe(4);
    for (const bad of [undefined, "", "0", "-1", "1.5", "01", "x", "9999999999"]) {
      expect(revisionParam(bad), String(bad)).toBeNull();
    }
  });
});

describe("splitRows", () => {
  const line = (kind: PlanDiffLine["kind"], old: number | null, now: number | null, text: string): PlanDiffLine => ({
    kind,
    old,
    new: now,
    text,
  });

  it("puts each removed run beside the added run after it, line by line", () => {
    const rows = splitRows({
      lines: [
        line("context", 1, 1, "a"),
        line("removed", 2, null, "b"),
        line("removed", 3, null, "c"),
        line("added", null, 2, "B"),
        line("context", 4, 3, "d"),
        line("added", null, 4, "e"),
        line("removed", 5, null, "f"),
      ],
    });
    expect(rows.map((row) => [row.kind, row.left?.text ?? null, row.right?.text ?? null])).toEqual([
      ["context", "a", "a"],
      ["change", "b", "B"],
      ["change", "c", null],
      ["context", "d", "d"],
      ["change", null, "e"],
      ["change", "f", null],
    ]);
  });
});
