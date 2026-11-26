import { describe, expect, it } from "vitest";

import type { RunEvent } from "./queries";
import { taskBoard } from "./run-task-board";
import { briefLines, commandName, eventsOfAgent, namesAgents, parentOf, reportAsk, runTasks, taskOf } from "./run-tasks";

let seq = 0;
function sys(subtype: string, body: Record<string, unknown>, at?: string): RunEvent {
  seq += 1;
  return {
    seq,
    at: at ?? `2026-10-22T07:${String(Math.floor(seq / 60)).padStart(2, "0")}:${String(seq % 60).padStart(2, "0")}.000Z`,
    kind: "output",
    body: { raw: { type: "system", subtype, session_id: "s1", ...body } },
    truncated: false,
  } as RunEvent;
}
function call(id: string, title: string, rawInput?: Record<string, unknown>, parent?: string): RunEvent {
  seq += 1;
  const body: Record<string, unknown> = { toolCallId: id, title, kind: title === "Bash" ? "execute" : "other", status: "pending" };
  if (rawInput) body.rawInput = rawInput;
  if (parent) body._meta = { parentToolCallId: parent };
  return { seq, at: "2026-10-22T07:00:00.000Z", kind: "tool_call", body, truncated: false } as RunEvent;
}
function result(id: string, text: string, parent?: string): RunEvent {
  seq += 1;
  const body: Record<string, unknown> = { toolCallId: id, status: "completed", content: [{ type: "content", content: { type: "text", text } }] };
  if (parent) body._meta = { parentToolCallId: parent };
  return { seq, at: "2026-10-22T07:05:00.000Z", kind: "tool_call_update", body, truncated: false } as RunEvent;
}

/** Run #545 of evo-agents in small: a plan-step subagent per step, its commands, one sent to the background, the
 * subagent woken again by it, and a background command of the main agent. */
function planRun(): RunEvent[] {
  seq = 0;
  return [
    call("toolu_agent1", "Agent"),
    sys("task_started", { task_id: "a1", task_type: "local_agent", description: "Org-chart step 1", subagent_type: "plan-step", tool_use_id: "toolu_agent1", is_backgrounded: false, prompt: "plan: org-chart" }),
    sys("task_progress", { task_id: "a1", description: "Running Read plan step and handoff", subagent_type: "plan-step", last_tool_name: "Bash", usage: { tool_uses: 1, total_tokens: 18563, duration_ms: 4535 } }),
    sys("task_started", { task_id: "b1", task_type: "local_bash", description: "Run migration tests", tool_use_id: "toolu_b1", parent_task_id: "a1", is_backgrounded: false, owned_by_subagent: true }),
    sys("task_notification", { task_id: "b1", status: "completed", summary: "Run migration tests", tool_use_id: "toolu_b1" }),
    sys("task_started", { task_id: "b2", task_type: "local_bash", description: "Run auth tests", tool_use_id: "toolu_b2", parent_task_id: "a1", is_backgrounded: false, owned_by_subagent: true }),
    sys("task_updated", { task_id: "b2", patch: { is_backgrounded: true } }),
    sys("task_progress", { task_id: "a1", description: "Running Wait for tests", last_tool_name: "Bash", usage: { tool_uses: 31, total_tokens: 105976 } }),
    sys("task_updated", { task_id: "a1", patch: { status: "completed", end_time: 1 } }),
    sys("task_notification", { task_id: "a1", status: "completed", summary: "The verify is still running. I'll wait.\nmore", tool_use_id: "toolu_agent1" }),
    // The subagent's background command wakes it: the same task starts again, in the background.
    sys("task_started", { task_id: "a1", task_type: "local_agent", description: "Org-chart step 1", subagent_type: "plan-step", tool_use_id: "toolu_agent1", is_backgrounded: true }),
    sys("task_started", { task_id: "m1", task_type: "local_bash", description: "Record step done", tool_use_id: "toolu_m1", is_backgrounded: true }),
    sys("task_started", { task_id: "f1", task_type: "local_bash", description: "Main foreground", tool_use_id: "toolu_f1", is_backgrounded: false }),
    sys("task_started", { task_id: "k1", task_type: "local_bash", description: "Killed watcher", tool_use_id: "toolu_k1", is_backgrounded: true }),
    sys("task_updated", { task_id: "k1", patch: { status: "killed", end_time: 2 } }),
    sys("task_notification", { task_id: "k1", status: "stopped", summary: "Killed watcher" }),
  ];
}

describe("run tasks", () => {
  it("follows each task from its start to its end, a subagent woken again running again", () => {
    const tasks = runTasks(planRun());
    const byId = new Map(tasks.map((task) => [task.id, task]));
    expect(tasks.map((task) => task.id)).toEqual(["a1", "b1", "b2", "m1", "f1", "k1"]);
    expect(byId.get("a1")).toMatchObject({
      kind: "agent",
      name: "Org-chart step 1",
      agentType: "plan-step",
      callId: "toolu_agent1",
      status: "running",
      endedAt: null,
      background: true,
      activity: "Wait for tests",
      lastTool: "Bash",
      toolUses: 31,
      tokens: 105976,
      summary: "The verify is still running. I'll wait.",
    });
    // It started at its first start, not when it was woken.
    expect(byId.get("a1")?.startedAt).toBe(planRun()[1].at);
    expect(byId.get("b1")).toMatchObject({ kind: "command", status: "completed", parent: "a1", background: false });
    expect(byId.get("b2")).toMatchObject({ status: "running", background: true, parent: "a1" });
    expect(byId.get("k1")).toMatchObject({ status: "stopped", background: true });
  });

  it("shows subagents with their commands and the background commands, not the main agent's foreground ones", () => {
    const board = taskBoard(runTasks(planRun()), true);
    expect(board.running.agents.map((row) => [row.task.id, row.children.map((child) => child.id)])).toEqual([["a1", ["b2"]]]);
    expect(board.running.commands.map((task) => task.id)).toEqual(["m1"]);
    expect(board).toMatchObject({ agents: 1, commands: 2 });
    // Ended: the killed background command; a subagent's foreground command is the subagent's business.
    expect(board.finished.commands.map((task) => task.id)).toEqual(["k1"]);
    expect(board.finished.agents).toEqual([]);
  });

  it("puts a subagent's background command on its own once the subagent ended", () => {
    const events = planRun();
    // A foreground command of the subagent whose end never came ends with it.
    events.push(sys("task_started", { task_id: "b3", task_type: "local_bash", description: "Wait", tool_use_id: "toolu_b3", parent_task_id: "a1", is_backgrounded: false }));
    events.push(sys("task_notification", { task_id: "a1", status: "completed", summary: "step: 1\nresult: pass" }));
    const board = taskBoard(runTasks(events), true);
    expect(board.running.agents).toEqual([]);
    expect(board.running.commands.map((task) => task.id)).toEqual(["b2", "m1"]);
    expect(board.finished.agents.map((task) => task.id)).toEqual(["a1"]);
    expect(board.finished.commands.map((task) => task.id)).toEqual(["k1"]);
    expect(board.finished.agents[0].summary).toBe("step: 1");
  });

  it("has nothing running once the run is no longer active", () => {
    const board = taskBoard(runTasks(planRun()), false);
    expect(board.running).toEqual({ agents: [], commands: [] });
    expect(board).toMatchObject({ agents: 0, commands: 0 });
  });

  it("reads events in seq order once each, and ignores what is not a task event", () => {
    const events = planRun();
    const shuffled = [...events].reverse().concat(events.slice(0, 3));
    expect(runTasks(shuffled)).toEqual(runTasks(events));
    seq = 100;
    expect(
      runTasks([
        { seq: 1, at: "2026-10-22T07:00:00Z", kind: "output", body: { raw: { type: "system", subtype: "init" } }, truncated: false } as RunEvent,
        { seq: 2, at: "2026-10-22T07:00:00Z", kind: "output", body: { raw: { type: "system", subtype: "task_started" } }, truncated: false } as RunEvent,
        { seq: 3, at: "2026-10-22T07:00:00Z", kind: "output", body: { raw: "text" }, truncated: false } as RunEvent,
        { seq: 4, at: "2026-10-22T07:00:00Z", kind: "system", body: { subtype: "task_started", task_id: "x" }, truncated: false } as RunEvent,
      ]),
    ).toEqual([]);
  });

  it("lists subagents and background commands apart, each command named by rule G1", () => {
    seq = 0;
    const events = [
      call("toolu_a", "Agent", { description: "Review the diff", subagent_type: "Explore", prompt: "Review it." }),
      sys("task_started", { task_id: "a", task_type: "local_agent", description: "Review the diff", subagent_type: "Explore", tool_use_id: "toolu_a" }),
      call("toolu_v", "Bash", { command: "cd ~/wt/evo && export UV_CACHE_DIR=/tmp/uv && export CI=1 && PYTHONPATH=src FOO='a b' uv run pytest -q tests", run_in_background: true }),
      sys("task_started", { task_id: "v", task_type: "local_bash", description: "Run the tests", tool_use_id: "toolu_v", is_backgrounded: true }),
      call("toolu_w", "Bash", { command: "bash -lc 'cd /srv && tail -f hub.log'", run_in_background: true }),
      sys("task_started", { task_id: "w", task_type: "local_bash", description: "Watch the log", tool_use_id: "toolu_w", is_backgrounded: true }),
      sys("task_notification", { task_id: "w", status: "stopped", summary: "Watch the log" }),
      call("toolu_b", "Agent", { description: "Write the docs", subagent_type: "general-purpose", prompt: "Docs." }),
      sys("task_started", { task_id: "b", task_type: "local_agent", description: "Write the docs", tool_use_id: "toolu_b" }),
      sys("task_notification", { task_id: "b", status: "completed", summary: "Docs written." }),
    ];
    const board = taskBoard(runTasks(events), true);
    expect(board.running.agents.map((row) => row.task.id)).toEqual(["a"]);
    expect(board.running.commands.map((task) => [task.id, task.command])).toEqual([["v", "uv run pytest -q tests"]]);
    expect(board.finished.agents.map((task) => task.id)).toEqual(["b"]);
    expect(board.finished.commands.map((task) => [task.id, task.command])).toEqual([["w", "tail -f hub.log"]]);
    // Rule G1: no `cd`, no chain of `export`s, no assignments before the program; all setup stays whole.
    expect(commandName("cd /a && export A=1 && export B=2 && make test")).toBe("make test");
    expect(commandName("A=1 B=\"x y\" pnpm lint")).toBe("pnpm lint");
    expect(commandName("cd /a && export A=1")).toBe("cd /a && export A=1");
  });

  it("holds a subagent's brief and its whole report, from its call or its notification", () => {
    seq = 0;
    const report = "step: 2\nresult: pass\n\nAll of it.";
    const events = [
      // In the foreground: the brief from the call, the report as the call's result, without Claude Code's trailer.
      call("toolu_f", "Agent", { description: "Step 2", subagent_type: "plan-step", prompt: "plan: x\nstep: 2\n\n## Report\nstep: <id>\nresult: pass | fail\n\n## Notes\nnone" }),
      sys("task_started", { task_id: "f", task_type: "local_agent", description: "Step 2", subagent_type: "plan-step", tool_use_id: "toolu_f" }),
      call("toolu_f1", "Bash", { command: "ls" }, "toolu_f"),
      result("toolu_f1", "a.txt", "toolu_f"),
      result("toolu_f", `${report}\n\nagentId: a1b2 (for resuming)\n<usage>total_tokens: 9</usage>`),
      sys("task_notification", { task_id: "f", status: "completed", summary: "Step 2 done" }),
      // In the background: the brief from task_started, the report as the notification's summary.
      call("toolu_g", "Agent", { description: "Scan", subagent_type: "Explore", run_in_background: true }),
      sys("task_started", { task_id: "g", task_type: "local_agent", description: "Scan", tool_use_id: "toolu_g", is_backgrounded: true, prompt: "Scan the repo." }),
      result("toolu_g", "Async agent launched successfully.\nagentId: g"),
      sys("task_notification", { task_id: "g", status: "completed", summary: "Found 3 places.\n\n- a.ts\n- b.ts" }),
      // A runtime with no task events: the call is the subagent.
      call("toolu_h", "Agent", { description: "Old style", subagent_type: "Explore", prompt: "Look." }),
    ];
    const tasks = runTasks(events);
    expect(tasks.map((task) => task.id)).toEqual(["f", "g", "toolu_h"]);
    expect(taskOf(tasks, "f")).toMatchObject({ prompt: expect.stringContaining("plan: x"), result: report, summary: "Step 2 done" });
    expect(taskOf(tasks, "toolu_g")).toMatchObject({ id: "g", prompt: "Scan the repo.", result: "Found 3 places.\n\n- a.ts\n- b.ts" });
    expect(taskOf(tasks, "toolu_h")).toMatchObject({ kind: "agent", name: "Old style", agentType: "Explore", status: "running", prompt: "Look.", result: null });
    // What the brief asks for, and its first lines for a card.
    expect(reportAsk(taskOf(tasks, "f")!.prompt)).toBe("## Report\nstep: <id>\nresult: pass | fail");
    expect(reportAsk("Just do it.")).toBeNull();
    expect(briefLines("plan: x\n\nstep: 2\nthird\nfourth", 3)).toEqual(["plan: x", "step: 2", "third"]);
    // Each event of the subagent names its call; the main agent's name none.
    expect(eventsOfAgent(events, "toolu_f").map((event) => event.seq)).toEqual([3, 4]);
    expect(eventsOfAgent(events, null)).toHaveLength(events.length - 2);
    expect(parentOf(events[2])).toBe("toolu_f");
    expect(namesAgents(events)).toBe(true);
    expect(namesAgents(planRun())).toBe(false);
  });
});
