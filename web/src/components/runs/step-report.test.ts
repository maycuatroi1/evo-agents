import { describe, expect, it } from "vitest";

import { parseStepReport, shellLines } from "./step-report";

// The shape of run #46's step 1 report, cut down.
const REPORT = [
  "step: 1",
  "result: pass",
  "commits: d4664cd",
  "branch: feat/worker-settings-web",
  "files: evo_agents/hub/migrations/versions/0026_worker_slots_16.py (new), evo_agents/hub/runs.py, web/src/lib/api/schema.d.ts",
  "outside_scope: none",
  'verify_command: ruff check . && python -m pytest -q tests/hub -k "worker and (settings or update)"; effective: ruff check . && PYTHON=python pnpm gen:api',
  "verify_output: |",
  "  Contracts: 5 kept, 0 broken.",
  "  ...............................                                          [100%]",
  "  31 passed in 1.29s",
  "decisions: |",
  "  Decision: The heartbeat answer key is `settings`: {slots, name, labels}.",
  "  Decision: PATCH body WorkerUpdate. A field that is left out stays as it is.",
  "notes: Step 2 must read answer[\"settings\"].",
  "In a worker plan run, `effective` needs `PYTHON=python`.",
].join("\n");

describe("parseStepReport", () => {
  it("reads every field of a step's report", () => {
    const found = parseStepReport(REPORT);
    expect(found).not.toBeNull();
    expect(found?.lead).toBe("");
    expect(found?.tail).toBe("");
    expect(found?.report).toEqual({
      step: "1",
      passed: true,
      commits: ["d4664cd"],
      commitsNote: null,
      branch: "feat/worker-settings-web",
      files: [
        { path: "evo_agents/hub/migrations/versions/0026_worker_slots_16.py", note: "new" },
        { path: "evo_agents/hub/runs.py", note: null },
        { path: "web/src/lib/api/schema.d.ts", note: null },
      ],
      outsideScope: [],
      verify: {
        command: 'ruff check . && python -m pytest -q tests/hub -k "worker and (settings or update)"',
        effective: "ruff check . && PYTHON=python pnpm gen:api",
      },
      verifyOutput: [
        "Contracts: 5 kept, 0 broken.",
        "...............................                                          [100%]",
        "31 passed in 1.29s",
      ].join("\n"),
      stash: null,
      decisions: [
        "The heartbeat answer key is `settings`: {slots, name, labels}.",
        "PATCH body WorkerUpdate. A field that is left out stays as it is.",
      ],
      notes: 'Step 2 must read answer["settings"].\nIn a worker plan run, `effective` needs `PYTHON=python`.',
    });
  });

  it("keeps the words around a fenced report, and reads a failed one", () => {
    const text = [
      "The verify failed, so I stopped:",
      "",
      "```yaml",
      "step: a2",
      "result: fail",
      "commits: none (the verify failed)",
      "branch: fix/x",
      "files: none",
      "outside_scope:",
      "  tests/conftest.py: a fixture the change made stale",
      "verify_command: pytest -q",
      "verify_output: |",
      "  E   AssertionError",
      "stash: stash@{0}",
      "decisions: |",
      "  Decision: none",
      "notes: none",
      "```",
      "",
      "Shall I go on?",
    ].join("\n");
    const found = parseStepReport(text);
    expect(found?.lead).toBe("The verify failed, so I stopped:");
    expect(found?.tail).toBe("Shall I go on?");
    expect(found?.report).toMatchObject({
      step: "a2",
      passed: false,
      commits: [],
      commitsNote: "none (the verify failed)",
      files: [],
      outsideScope: ["tests/conftest.py: a fixture the change made stale"],
      verify: { command: "pytest -q", effective: null },
      verifyOutput: "E   AssertionError",
      stash: "stash@{0}",
      decisions: [],
      notes: null,
    });
  });

  it("reads a report that is still arriving", () => {
    expect(parseStepReport("step: 3\nresult: pass\ncomm")?.report).toMatchObject({ step: "3", passed: true, commits: [], branch: null });
  });

  it("leaves other messages alone", () => {
    expect(parseStepReport("Step 1 is running in a subagent.")).toBeNull();
    expect(parseStepReport("step: 1\nThe result comes later.")).toBeNull();
    expect(parseStepReport("Next: step: 2\nresult: pass")).toBeNull();
    expect(parseStepReport("")).toBeNull();
  });
});

describe("shellLines", () => {
  it("puts each command of a long chain on its own line, outside quotes", () => {
    const command = 'ruff check . && ruff format --check . && python -m pytest -q tests -k "a && b" || echo failed; cd web && pnpm gen:api';
    expect(shellLines(command)).toBe(
      [
        "ruff check . &&",
        "  ruff format --check . &&",
        '  python -m pytest -q tests -k "a && b" ||',
        "  echo failed;",
        "  cd web &&",
        "  pnpm gen:api",
      ].join("\n"),
    );
  });

  it("leaves a short command on one line", () => {
    expect(shellLines("ruff check . && pytest -q")).toBe("ruff check . && pytest -q");
  });
});
