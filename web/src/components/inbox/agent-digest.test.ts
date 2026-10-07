import { describe, expect, it } from "vitest";

import type { RunEvent } from "@/components/runs/queries";
import { buildTrace, type TraceEvent } from "@/components/runs/trace-model";
import claudeCode from "@/test/fixtures/trace/claude-code.json";

import { agentDigest, DIGEST_CHARS, DIGEST_ITEMS, oneLine, plainText } from "./agent-digest";

let seq = 0;
const at = (second: number) => `2026-10-07T03:00:${String(second).padStart(2, "0")}.000Z`;
function ev(kind: string, body: Record<string, unknown>): TraceEvent {
  seq += 1;
  return { seq, at: at(seq), kind: kind as RunEvent["kind"], body };
}

/** A run that reads, tests, plans, says what it found and asks decision 7, with its moves between states. */
function run(): TraceEvent[] {
  seq = 0;
  return [
    ev("state", { from: "leased", to: "running" }),
    ev("agent_message_chunk", { content: { type: "text", text: "Reading the **plan** first." } }),
    ev("tool_call", { toolCallId: "t1", title: "Read", kind: "read", rawInput: { file_path: "plans/active/rollout.yaml" } }),
    ev("tool_call_update", { toolCallId: "t1", status: "completed" }),
    ev("tool_call", { toolCallId: "t2", title: "Bash", kind: "execute", rawInput: { command: "pnpm test" } }),
    ev("tool_call_update", { toolCallId: "t2", status: "failed", rawOutput: { exit_code: 1 } }),
    ev("plan", { entries: [{ content: "Test", status: "completed" }, { content: "Deploy", status: "pending" }] }),
    ev("agent_message_chunk", { content: { type: "text", text: "Tests fail on `__init__.py`;\n\nsee the log." } }),
    ev("system", { text: "Asked you: Deploy now?", decision: { id: 7, category: "deploy", step: "3" } }),
    ev("state", { from: "running", to: "waiting" }),
  ];
}

describe("agentDigest", () => {
  it("keeps the last five things the agent did, oldest first, without moves or the decision on screen", () => {
    const lines = agentDigest(buildTrace(run()), 7);
    expect(lines).toHaveLength(DIGEST_ITEMS);
    expect(lines.map((line) => line.type)).toEqual(["agent", "tool", "tool", "plan", "agent"]);
    const [said, read, bash, plan, last] = lines;
    expect(said).toMatchObject({ type: "agent", text: "Reading the plan first." });
    expect(read).toMatchObject({ type: "tool", kind: "read", name: "Read", arg: "plans/active/rollout.yaml", failed: false });
    expect(bash).toMatchObject({ type: "tool", kind: "execute", name: "Bash", arg: "pnpm test", failed: true });
    expect(plan).toMatchObject({ type: "plan", done: 1, total: 2 });
    // Paragraphs on one line; an identifier's underscores kept.
    expect(last).toMatchObject({ type: "agent", text: "Tests fail on __init__.py; see the log." });
  });

  it("shows another decision the agent asked, and fewer lines when the trace has fewer", () => {
    const lines = agentDigest(buildTrace(run()), 8, 10);
    expect(lines.map((line) => line.type)).toEqual(["agent", "tool", "tool", "plan", "agent", "ask"]);
    expect(lines.at(-1)).toMatchObject({ type: "ask", decisionId: 7 });
  });

  it("reads a real Claude Code run", () => {
    const lines = agentDigest(buildTrace((claudeCode as { events: RunEvent[] }).events), 1);
    expect(lines.length).toBeGreaterThan(0);
    expect(lines.length).toBeLessThanOrEqual(DIGEST_ITEMS);
    for (const line of lines) expect(line.key).toBeTruthy();
  });

  it("is empty for a run that has only moved between states", () => {
    seq = 0;
    expect(agentDigest(buildTrace([ev("state", { from: "queued", to: "leased" })]), 7)).toEqual([]);
  });
});

describe("plainText and oneLine", () => {
  it("drops Markdown's marks and keeps the words", () => {
    expect(plainText("## Done\n\n- [x] **Bumped** the *version*, see [the log](https://example.org).\n> `make` ran")).toBe(
      "Done\n\nBumped the version, see the log.\nmake ran",
    );
    expect(plainText("snake_case and __init__.py stay")).toBe("snake_case and __init__.py stay");
  });

  it("puts text on one line, cut with an ellipsis", () => {
    expect(oneLine("  one\n\ntwo  ")).toBe("one two");
    const long = oneLine("x".repeat(DIGEST_CHARS + 20));
    expect(long).toHaveLength(DIGEST_CHARS);
    expect(long.endsWith("…")).toBe(true);
  });
});
