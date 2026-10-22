import { describe, expect, it } from "vitest";

import type { RunEvent } from "./queries";
import { runTasks, taskBoard } from "./run-tasks";

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
function call(id: string, title: string): RunEvent {
  seq += 1;
  return { seq, at: "2026-10-22T07:00:00.000Z", kind: "tool_call", body: { toolCallId: id, title, kind: "other", status: "pending" }, truncated: false } as RunEvent;
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
    expect(board.running.map((row) => [row.task.id, row.children.map((child) => child.id)])).toEqual([
      ["a1", ["b2"]],
      ["m1", []],
    ]);
    expect(board).toMatchObject({ agents: 1, commands: 2 });
    // Ended: the killed background command; a subagent's foreground command is the subagent's business.
    expect(board.finished.map((task) => task.id)).toEqual(["k1"]);
  });

  it("puts a subagent's background command on its own once the subagent ended", () => {
    const events = planRun();
    // A foreground command of the subagent whose end never came ends with it.
    events.push(sys("task_started", { task_id: "b3", task_type: "local_bash", description: "Wait", tool_use_id: "toolu_b3", parent_task_id: "a1", is_backgrounded: false }));
    events.push(sys("task_notification", { task_id: "a1", status: "completed", summary: "step: 1\nresult: pass" }));
    const board = taskBoard(runTasks(events), true);
    expect(board.running.map((row) => row.task.id)).toEqual(["b2", "m1"]);
    expect(board.finished.map((task) => task.id)).toEqual(["a1", "k1"]);
    expect(board.finished[0].summary).toBe("step: 1");
  });

  it("has nothing running once the run is no longer active", () => {
    const board = taskBoard(runTasks(planRun()), false);
    expect(board.running).toEqual([]);
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
});
