import { describe, expect, it } from "vitest";

import claudeCode from "@/test/fixtures/trace/claude-code.json";
import codex from "@/test/fixtures/trace/codex.json";
import fakeClaudeCode from "@/test/fixtures/trace/fake-claude-code.json";
import opencode from "@/test/fixtures/trace/opencode.json";

import type { RunEvent } from "./queries";
import {
  buildTrace,
  cutOutput,
  diffStat,
  openByDefault,
  type ToolItem,
  toolArgument,
  type TraceEvent,
  type TraceItem,
  unwrapShell,
} from "./trace-model";

/**
 * The fixtures are events of real runs of the three runtimes' adapters in a scratch repository, and the fake adapter's
 * samples of Claude Code (web/scripts/trace_fixtures.py): the agent runs a command that fails, one that succeeds,
 * writes a file and answers.
 */

type Fixture = { events: RunEvent[] };

const fixtures = { "fake claude-code": fakeClaudeCode, "claude-code": claudeCode, opencode, codex } as Record<string, Fixture>;

const tools = (items: TraceItem[]) => items.filter((item): item is ToolItem => item.type === "tool");
const types = (items: TraceItem[]) => items.map((item) => item.type);

let seq = 0;
const at = (second: number) => `2026-10-07T03:00:${String(second).padStart(2, "0")}.000Z`;
function ev(kind: string, body: Record<string, unknown>, second = seq): TraceEvent {
  seq += 1;
  return { seq, at: at(second), kind: kind as RunEvent["kind"], body };
}
function fresh() {
  seq = 0;
}

describe("buildTrace on real runs", () => {
  it.each(Object.entries(fixtures))("makes one row of each tool call of %s, its call and updates matched by toolCallId", (_name, fixture) => {
    const items = buildTrace(fixture.events);
    const calls = new Set(fixture.events.filter((event) => event.kind === "tool_call").map((event) => event.body.toolCallId));
    const rows = tools(items);
    expect(rows.map((row) => row.tool.id)).toEqual([...calls]);
    for (const row of rows) {
      expect(row.tool.status).toMatch(/^(completed|failed)$/);
      expect(row.tool.ms).not.toBeNull();
      expect(row.tool.arg).not.toBeNull();
    }
    // Nothing is lost: every event is in one item, or is an update or a usage report the rows and the usage card read.
    const shown = items.flatMap((item) => (item.type === "raw" ? item.events.map((event) => event.seq) : [item.seq]));
    expect(new Set(shown).size).toBe(shown.length);
  });

  it("reads Claude Code's failed command as failed, with its exit code from the text, open, and timed from the events", () => {
    const rows = tools(buildTrace(claudeCode.events as RunEvent[]));
    const failed = rows[0].tool;
    expect(failed).toMatchObject({ title: "Bash", kind: "execute", arg: "cat missing-file.txt", status: "failed", exitCode: 1 });
    expect(failed.description).toBe("Read missing file (expected to fail)");
    expect(failed.output).toContain("No such file or directory");
    expect(failed.ms).toBe(Date.parse("2026-10-07T03:00:07.336Z") - Date.parse("2026-10-07T03:00:05.854Z"));
    expect(openByDefault(failed)).toBe(true);
    expect(rows.slice(1).every((row) => !openByDefault(row.tool))).toBe(true);
  });

  it("reads Codex's command as its own line without the shell around it, and its exit code from rawOutput", () => {
    const rows = tools(buildTrace(codex.events as RunEvent[]));
    expect(rows.map((row) => row.tool.arg)).toEqual(["cat missing-file.txt", "ls -a", "printf 'ok' > selftest.txt"]);
    expect(rows.map((row) => row.tool.exitCode)).toEqual([1, 0, 0]);
    expect(rows[0].tool.status).toBe("failed");
    expect(rows[2].tool.output).toBeNull(); // the command printed nothing; rawOutput holds only its exit and duration
  });

  it("folds opencode's thinking into the step that follows it, timed from the event before it", () => {
    const items = buildTrace(opencode.events as RunEvent[]);
    const agents = items.filter((item) => item.type === "agent");
    expect(agents).toHaveLength(4);
    // Three thoughts before a tool call stand alone; the last folds into the answer.
    expect(agents.slice(0, 3).every((item) => item.type === "agent" && item.text === "" && item.thought !== null)).toBe(true);
    const last = agents[3];
    expect(last.type === "agent" && last.text).toContain("selftest.txt");
    expect(last.type === "agent" && last.thought?.text).toBe("Done. Reply with one short sentence.");
    // From the event before it (opencode's step event at 28.511) to the thought (31.806).
    expect(last.type === "agent" && last.thought?.ms).toBe(3295);
    expect(tools(items)[0].tool).toMatchObject({ title: "bash", status: "failed", exitCode: 1 });
  });

  it("ends a turn at a usage report, so Claude Code's two turns are two messages", () => {
    const items = buildTrace(claudeCode.events as RunEvent[]);
    const messages = items.filter((item) => item.type === "agent").map((item) => (item.type === "agent" ? item.text : ""));
    expect(messages).toEqual(["STARTED", expect.stringContaining("selftest.txt")]);
    fresh();
    const turns = buildTrace([ev("agent_message_chunk", { text: "One." }), ev("usage_update", { usage: { input_tokens: 1 } }), ev("agent_message_chunk", { text: "Two." })]);
    expect(turns).toMatchObject([
      { type: "agent", text: "One." },
      { type: "agent", text: "Two." },
    ]);
  });

  it("folds the runtime's events no adapter reads into one raw row per run of them", () => {
    const items = buildTrace(claudeCode.events as RunEvent[]);
    const raws = items.filter((item) => item.type === "raw");
    expect(raws[0].type === "raw" && raws[0].events.map((event) => event.label)).toEqual([
      "system hook_response",
      "system hook_response",
      "system hook_response",
      "system init",
    ]);
    expect(raws[0].type === "raw" && raws[0].events[3].json).toContain('"subtype": "init"');
    expect(types(items).slice(0, 4)).toEqual(["move", "move", "raw", "tool"]);
    expect(types(items).slice(-2)).toEqual(["move", "move"]);
  });
});

describe("buildTrace on events out of order and of unknown shape", () => {
  it("reads events in seq order whatever order they come in, each seq once", () => {
    const events = codex.events as RunEvent[];
    const shuffled = [...events].reverse();
    shuffled.splice(3, 0, events[5], events[9]); // two of them twice
    expect(buildTrace(shuffled)).toEqual(buildTrace(events));
  });

  it("makes the row from an update that comes before its call, and fills it in when the call comes", () => {
    fresh();
    const items = buildTrace([
      ev("tool_call_update", { toolCallId: "c1", status: "completed", content: [{ type: "content", content: { type: "text", text: "3 passed" } }] }, 9),
      ev("tool_call", { toolCallId: "c1", title: "Bash", kind: "execute", status: "pending", rawInput: { command: "pytest -q" } }, 4),
    ]);
    expect(items).toHaveLength(1);
    expect((items[0] as ToolItem).tool).toMatchObject({ title: "Bash", arg: "pytest -q", status: "completed", output: "3 passed", ms: 5000 });
  });

  it("keeps the latest status and output of a call updated more than once, and its duration to the final update", () => {
    fresh();
    const items = buildTrace([
      ev("tool_call", { toolCallId: "c2", title: "Bash", rawInput: { command: "make" }, status: "pending" }, 1),
      ev("tool_call_update", { toolCallId: "c2", status: "in_progress", content: [{ type: "content", content: { type: "text", text: "building" } }] }, 2),
      ev("tool_call_update", { toolCallId: "c2", status: "failed", rawOutput: { exitCode: 2, stderr: "no rule" } }, 7),
    ]);
    const tool = (items[0] as ToolItem).tool;
    expect(tool).toMatchObject({ kind: "execute", status: "failed", exitCode: 2, ms: 6000, output: "no rule" });
    expect(openByDefault(tool)).toBe(true);
  });

  it("joins a run of message chunks into one message, which a tool update does not break", () => {
    fresh();
    const items = buildTrace([
      ev("tool_call", { toolCallId: "c3", title: "Read", rawInput: { file_path: "a.ts" } }, 1),
      ev("agent_message_chunk", { content: { type: "text", text: "First." } }, 2),
      ev("tool_call_update", { toolCallId: "c3", status: "completed" }, 2),
      ev("agent_message_chunk", { text: "Second." }, 3),
      ev("user_message", { text: "go on", from: "octo", message_id: 4 }, 4),
      ev("agent_message_chunk", { content: { type: "text", text: "Third." } }, 5),
    ]);
    expect(types(items)).toEqual(["tool", "agent", "user", "agent"]);
    expect(items[1]).toMatchObject({ type: "agent", text: "First.\n\nSecond." });
    expect(items[2]).toMatchObject({ type: "user", text: "go on", from: "octo", decisionId: null });
  });

  it("says a decision the agent asked, a move, a verify line, a plan, and an answer, each in its own row", () => {
    fresh();
    const items = buildTrace([
      ev("plan", { entries: [{ content: "Bump the version", status: "completed", priority: "high" }, { content: "Publish", status: "in_progress" }, "Tag"] }),
      ev("system", { text: "decision #7 asked (scope): Publish evo-agents 0.4.0 to PyPI now?", decision: { id: 7, step: "13", category: "scope" } }),
      ev("state", { from: "running", to: "waiting", actor: "worker", reason: null }),
      ev("user_message", { text: "Decision #7: Publish now", from: "octo", decision_id: 7 }),
      ev("system", { text: "verify: `pnpm test` exited 1 after 900 ms", exit_code: 1, output: "FAIL a.test.ts\n" }),
    ]);
    expect(types(items)).toEqual(["plan", "ask", "move", "user", "system"]);
    expect(items[0]).toMatchObject({
      entries: [
        { content: "Bump the version", status: "completed", priority: "high" },
        { content: "Publish", status: "in_progress", priority: null },
        { content: "Tag", status: "pending", priority: null },
      ],
    });
    expect(items[1]).toMatchObject({ decisionId: 7, question: "Publish evo-agents 0.4.0 to PyPI now?", category: "scope", step: "13" });
    expect(items[2]).toMatchObject({ move: { from: "running", to: "waiting" } });
    expect(items[3]).toMatchObject({ decisionId: 7 });
    expect(items[4]).toMatchObject({ tone: "error", output: "FAIL a.test.ts" });
  });

  it("folds an event of a kind it does not know, and a body of no known shape, into the raw row", () => {
    fresh();
    const items = buildTrace([
      ev("output", { raw: { method: "hook/completed", params: { x: 1 } } }),
      ev("session_mode_update", { mode: "plan" }),
      ev("tool_call", { status: "pending" }),
      ev("agent_thought_chunk", "not an object" as unknown as Record<string, unknown>),
    ]);
    expect(types(items)).toEqual(["raw", "tool", "agent"]);
    expect(items[0].type === "raw" && items[0].events.map((event) => event.label)).toEqual(["hook/completed", "session_mode_update"]);
    expect((items[1] as ToolItem).tool).toMatchObject({ id: null, title: null, kind: "other", arg: null, status: "pending" });
  });

  it("gives an update without an id to the latest call still without a result", () => {
    fresh();
    const items = buildTrace([
      ev("tool_call", { toolCallId: "a", title: "Bash", rawInput: { command: "make" }, status: "in_progress" }, 1),
      ev("tool_call", { toolCallId: "b", title: "Bash", rawInput: { command: "make test" }, status: "in_progress" }, 2),
      ev("tool_call_update", { status: "failed", rawOutput: "1 failed" }, 3),
      ev("tool_call_update", { status: "completed", rawOutput: "built" }, 4),
      ev("tool_call_update", { status: "completed", rawOutput: "nobody's" }, 5),
    ]);
    expect(tools(items).map((row) => [row.tool.id, row.tool.status, row.tool.output])).toEqual([
      ["a", "completed", "built"],
      ["b", "failed", "1 failed"],
      [null, "completed", "nobody's"],
    ]);
  });

  it("never times a call below zero when the hub's and the worker's clocks disagree", () => {
    fresh();
    const items = buildTrace([
      ev("tool_call", { toolCallId: "c4", title: "Bash", rawInput: { command: "true" } }, 10),
      ev("tool_call_update", { toolCallId: "c4", status: "completed" }, 8),
    ]);
    expect((items[0] as ToolItem).tool.ms).toBe(0);
  });
});

describe("tool rows", () => {
  it("names the main argument of a call in one line", () => {
    expect(toolArgument({ command: "pytest -q\nmore", description: "Run the tests" })).toBe("pytest -q …");
    expect(toolArgument({ file_path: "/src/a.ts", offset: 3 })).toBe("/src/a.ts");
    expect(toolArgument({ pattern: "TODO", path: "src" })).toBe("TODO");
    expect(toolArgument({ path: "src" })).toBe("src");
    expect(toolArgument({ changes: [{ path: "a.ts" }, { path: "b.ts" }] })).toBe("a.ts, b.ts");
    expect(toolArgument({ todos: [] })).toBeNull();
    expect(toolArgument(null)).toBeNull();
    expect(unwrapShell("/bin/zsh -lc 'ls -a'")).toBe("ls -a");
    expect(unwrapShell(`bash -c "echo 'x'"`)).toBe("echo 'x'");
    expect(unwrapShell("ls -a")).toBe("ls -a");
  });

  it("counts an edit's lines from Claude Code's, opencode's and Codex's input, or ACP's diff content", () => {
    expect(diffStat({ file_path: "a", old_string: "a\nb", new_string: "a\nb\nc" }, undefined)).toEqual({ added: 3, removed: 2 });
    expect(diffStat({ filePath: "a", oldString: "x", newString: "y" }, undefined)).toEqual({ added: 1, removed: 1 });
    expect(diffStat({ file_path: "a", content: "1\n2\n3\n" }, undefined)).toEqual({ added: 3, removed: 0 });
    expect(diffStat({ changes: [{ path: "a", diff: "--- a\n+++ b\n@@\n-old\n+new\n+more" }] }, undefined)).toEqual({ added: 2, removed: 1 });
    expect(diffStat({}, [{ type: "diff", path: "a", oldText: null, newText: "x\ny" }])).toEqual({ added: 2, removed: 0 });
    expect(diffStat({ command: "ls" }, undefined)).toBeNull();
  });

  it("shows an edit's change and stat on its row", () => {
    fresh();
    const items = buildTrace([
      ev("tool_call", { toolCallId: "e1", title: "Edit", kind: "edit", rawInput: { file_path: "pyproject.toml", old_string: 'version = "0.3.0"', new_string: 'version = "0.4.0"' } }),
      ev("tool_call_update", { toolCallId: "e1", status: "completed", content: [{ type: "content", content: { type: "text", text: "The file was updated." } }] }),
    ]);
    expect((items[0] as ToolItem).tool).toMatchObject({
      arg: "pyproject.toml",
      diff: { added: 1, removed: 1 },
      change: '- version = "0.3.0"\n+ version = "0.4.0"',
      output: "The file was updated.",
    });
  });

  it("cuts an output at 20 lines and says how many it has", () => {
    const text = Array.from({ length: 45 }, (_, index) => `line ${index + 1}`).join("\n");
    expect(cutOutput(text)).toEqual({ shown: text.split("\n").slice(0, 20).join("\n"), total: 45, cut: true });
    expect(cutOutput("one\ntwo\n")).toEqual({ shown: "one\ntwo", total: 2, cut: false });
  });
});
