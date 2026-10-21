import { describe, expect, it } from "vitest";

import type { RunEvent } from "./queries";
import { callFailed, groupByIntent, groupObjects, intentOf, programOf } from "./trace-groups";
import { buildTrace, type ToolCall, type TraceEvent } from "./trace-model";

let seq = 0;
function ev(kind: string, body: Record<string, unknown>): TraceEvent {
  seq += 1;
  return { seq, at: `2026-10-17T13:06:${String(seq).padStart(2, "0")}.000Z`, kind: kind as RunEvent["kind"], body };
}
const WT = "cd /home/me/.evo/worker/worktrees/demo-80/api && export PATH=/tmp/venv/bin:$PATH";
const bash = (id: string, command: string, update: Record<string, unknown> = { status: "completed", rawOutput: "ok" }) => [
  ev("tool_call", { toolCallId: id, title: "Bash", kind: "execute", rawInput: { command } }),
  ev("tool_call_update", { toolCallId: id, ...update }),
];
const call = (command: string, kind: ToolCall["kind"] = "execute") => ({ kind, arg: command, title: "Bash" });

describe("trace groups", () => {
  it("names the work a call does from the program its command runs", () => {
    expect(programOf(`${WT} && timeout 580 python -m pytest -q`)).toBe("python -m pytest -q");
    expect(intentOf(call(`${WT} && sed -n 1,20p a.py`))).toBe("explore");
    expect(intentOf(call("git diff --stat"))).toBe("explore");
    expect(intentOf(call(`${WT} && timeout 580 python -m pytest -q tests/hub`))).toBe("test");
    expect(intentOf(call("cd web && pnpm exec vitest run src"))).toBe("test");
    expect(intentOf(call("ruff check . 2>&1 | tail -3"))).toBe("check");
    expect(intentOf(call("cd web && pnpm typecheck"))).toBe("check");
    expect(intentOf(call("git commit -m 'x'"))).toBe("git");
    expect(intentOf(call("gh pr create --fill"))).toBe("github");
    expect(intentOf(call("evo-agents worker step 7 done"))).toBe("hub");
    expect(intentOf(call("make build"))).toBe("run");
    expect(intentOf(call("/src/a.ts", "read"))).toBe("explore");
    expect(intentOf(call("/src/a.ts", "edit"))).toBe("edit");
    expect(intentOf({ kind: "other", arg: null, title: "TodoWrite" })).toBe("tool");
  });

  it("puts calls of the same work in a row in one group, which bookkeeping does not cut and words do", () => {
    seq = 0;
    const items = buildTrace([
      ...bash("a", `${WT} && sed -n 1,20p evo_agents/hub/plugins.py`),
      ev("output", { type: "system", subtype: "task_progress" }),
      ...bash("b", `${WT} && grep -rn "plugin" evo_agents/hub/server/`, { status: "failed", rawOutput: { exitCode: 1 } }),
      ...bash("c", `${WT} && cat docs/plugins.md`),
      ev("agent_message_chunk", { content: { type: "text", text: "Now the tests." } }),
      ...bash("d", `${WT} && pytest -q tests/hub/test_plugins.py`, { status: "failed", rawOutput: { exitCode: 1 } }),
      ...bash("e", `${WT} && pytest -q tests/hub/test_plugins.py`),
    ]);
    const shown = groupByIntent(items);
    expect(shown.map((item) => (item.type === "group" ? `${item.intent}:${item.calls.length}` : item.type))).toEqual(["explore:3", "agent", "test:2"]);
    const [explore, , tests] = shown;
    // A search that finds nothing is no failure; a test that exits 1 is.
    expect(explore.type === "group" && explore.calls.filter(({ tool }) => callFailed(tool))).toHaveLength(0);
    expect(tests.type === "group" && tests.calls.filter(({ tool }) => callFailed(tool))).toHaveLength(1);
    expect(explore.type === "group" && groupObjects(explore)).toEqual(["plugins.py", "server", "plugins.md"]);
    // A group keeps its first call's key as calls join it.
    expect(explore.key).toBe(`g${items[0].key}`);
  });
});
