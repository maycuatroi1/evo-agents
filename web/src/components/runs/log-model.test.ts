import { describe, expect, it } from "vitest";

import {
  contentText,
  filterLines,
  groupCounts,
  KIND_GROUP,
  MAX_LINE_CHARS,
  moveOf,
  splitMatches,
  toLogLine,
} from "./log-model";
import type { RunEvent } from "./queries";

const describe_ = (move: { from: string | null; to: string; actor: string | null; reason: string | null }) =>
  `${move.from ?? "?"}>${move.to}${move.actor ? ` by ${move.actor}` : ""}${move.reason ? `: ${move.reason}` : ""}`;

function event(seq: number, kind: RunEvent["kind"], body: Record<string, unknown>, truncated = false): RunEvent {
  return { seq, at: `2026-10-05T07:00:${String(seq).padStart(2, "0")}Z`, kind, body, truncated };
}

describe("toLogLine", () => {
  it("reads the agent's text in its common shapes", () => {
    expect(toLogLine(event(1, "agent_message_chunk", { text: "Reading the plan." }), describe_).text).toBe("Reading the plan.");
    expect(toLogLine(event(2, "agent_message_chunk", { content: { type: "text", text: "ACP block" } }), describe_).text).toBe("ACP block");
    expect(toLogLine(event(3, "agent_thought_chunk", { content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] }), describe_).text).toBe(
      "a\nb",
    );
  });

  it("says what a tool was asked, and marks a failed result", () => {
    const call = toLogLine(event(4, "tool_call", { toolCallId: "t1", title: "Bash", rawInput: { command: "pytest -q" }, status: "pending" }), describe_);
    expect(call).toMatchObject({ text: "Bash: pytest -q", group: "tools", tone: null });
    const read = toLogLine(event(5, "tool_call", { name: "Read", input: { file_path: "src/app.ts" } }), describe_);
    expect(read.text).toBe("Read: src/app.ts");
    const failed = toLogLine(
      event(6, "tool_call_update", { toolCallId: "t1", status: "failed", content: [{ type: "content", content: { type: "text", text: "1 failed" } }] }),
      describe_,
    );
    expect(failed).toMatchObject({ text: "[failed] 1 failed", tone: "error" });
    expect(toLogLine(event(7, "tool_call_update", { status: "completed", rawOutput: "ok" }), describe_).text).toBe("[completed] ok");
  });

  it("writes the agent's plan one entry per line, with its status", () => {
    const line = toLogLine(
      event(8, "plan", { entries: [{ content: "Read", status: "completed" }, { content: "Write", status: "in_progress" }] }),
      describe_,
    );
    expect(line.text).toBe("[completed] Read\n[in_progress] Write");
  });

  it("tones a verify result by its exit code and keeps its output", () => {
    const passed = toLogLine(event(9, "system", { text: "verify: `pnpm test` exited 0 after 900 ms", exit_code: 0, output: "12 passed\n" }), describe_);
    expect(passed).toMatchObject({ tone: "ok", group: "system" });
    expect(passed.text).toBe("verify: `pnpm test` exited 0 after 900 ms\n12 passed");
    expect(toLogLine(event(10, "system", { text: "verify", exit_code: 1 }), describe_).tone).toBe("error");
  });

  it("names who sent a message, and describes a move in the page's words", () => {
    expect(toLogLine(event(11, "user_message", { text: "also cover revoked tokens", from: "octo", message_id: 3 }), describe_)).toMatchObject({
      text: "octo: also cover revoked tokens",
      group: "agent",
    });
    const state = toLogLine(event(12, "state", { from: "verifying", to: "review", actor: "worker", reason: null }), describe_);
    expect(state).toMatchObject({ text: "verifying>review by worker", tone: "ok", group: "system" });
    expect(toLogLine(event(13, "state", { from: "running", to: "failed", actor: "reaper", reason: "timeout" }), describe_).tone).toBe("error");
  });

  it("flattens usage, falls back to the body's JSON, and shortens a long line", () => {
    expect(toLogLine(event(14, "usage_update", { input_tokens: 1200, cache: { read: 3 } }), describe_).text).toBe("input_tokens 1200, cache.read 3");
    expect(toLogLine(event(15, "output", { type: "weird", n: 1 }), describe_).text).toBe('{"type":"weird","n":1}');
    const long = toLogLine(event(16, "agent_message_chunk", { text: "x".repeat(MAX_LINE_CHARS + 5) }, true), describe_);
    expect(long.text).toHaveLength(MAX_LINE_CHARS);
    expect(long).toMatchObject({ shortened: true, truncated: true });
  });

  it("puts every kind in a group", () => {
    expect(Object.keys(KIND_GROUP).sort()).toEqual(
      ["agent_message_chunk", "agent_thought_chunk", "output", "plan", "state", "system", "tool_call", "tool_call_update", "usage_update", "user_message"],
    );
  });
});

describe("moves, filters and matches", () => {
  it("reads a state event as a move, and nothing else", () => {
    expect(moveOf(event(1, "state", { from: "queued", to: "leased", actor: "worker", reason: null }))).toEqual({
      seq: 1,
      at: "2026-10-05T07:00:01Z",
      from: "queued",
      to: "leased",
      actor: "worker",
      reason: null,
    });
    expect(moveOf(event(2, "system", { to: "x" }))).toBeNull();
    expect(moveOf(event(3, "state", {}))).toBeNull();
  });

  it("filters by group and by text in any case, and counts each group", () => {
    const lines = [
      toLogLine(event(1, "agent_message_chunk", { text: "Reading the PLAN" }), describe_),
      toLogLine(event(2, "tool_call", { title: "Read", rawInput: { path: "plan.yaml" } }), describe_),
      toLogLine(event(3, "system", { text: "worktree ready" }), describe_),
    ];
    expect(filterLines(lines, null, "")).toBe(lines);
    expect(filterLines(lines, null, "plan").map((line) => line.seq)).toEqual([1, 2]);
    expect(filterLines(lines, "tools", "plan").map((line) => line.seq)).toEqual([2]);
    expect(filterLines(lines, "system", "plan")).toEqual([]);
    expect(groupCounts(lines)).toEqual({ agent: 1, tools: 1, output: 0, system: 1 });
  });

  it("splits text around its matches for highlighting", () => {
    expect(splitMatches("Run the Tests, then test again", "test")).toEqual(["Run the ", "Test", "s, then ", "test", " again"]);
    expect(splitMatches("İstanbul", "stan")).toEqual(["İstanbul"]); // lower case changes its length: no highlight
    expect(splitMatches("nothing here", "zz")).toEqual(["nothing here"]);
    expect(splitMatches("whole", "  ")).toEqual(["whole"]);
  });

  it("reads content blocks of every ACP shape", () => {
    expect(contentText({ type: "diff", path: "a.ts" })).toBe("diff a.ts");
    expect(contentText([{ type: "content", content: { type: "text", text: "x" } }, { type: "image" }])).toBe("x");
    expect(contentText(42)).toBeNull();
  });
});
