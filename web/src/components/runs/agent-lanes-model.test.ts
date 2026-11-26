import { describe, expect, it } from "vitest";

import { buildLanes, stepStart } from "./agent-lanes-model";
import type { RunEvent } from "./queries";
import type { RunTask } from "./run-tasks";

const at = (minute: number) => new Date(Date.UTC(2026, 10, 26, 8, minute)).toISOString();
const ev = (seq: number, minute: number, kind: string, body: Record<string, unknown> = {}) => ({ seq, at: at(minute), kind, body, truncated: false }) as RunEvent;

function task(over: Partial<RunTask>): RunTask {
  return {
    id: "a1", kind: "agent", type: "local_agent", name: "Step 5", agentType: "plan-step", parent: null, background: false, callId: "c1", status: "completed",
    startedAt: at(2), endedAt: at(8), activity: null, lastTool: null, toolUses: null, tokens: null, summary: "done", prompt: null, result: null, command: null, firstSeq: 1, seq: 2,
    ...over,
  };
}

const events = [ev(1, 0, "agent_message_chunk"), ev(2, 10, "tool_call", { title: "Bash", rawInput: { command: "ls", description: "List" } })];

describe("agent lanes", () => {
  it("draws nothing for a run with no sub-agent and no background command", () => {
    expect(buildLanes(events, [], false, null, "run")).toBeNull();
  });

  it("splits the main agent's time into work and waiting on a foreground sub-agent, with a mark each way", () => {
    const board = buildLanes(events, [task({})], false, null, "run");
    const main = board?.lanes[0];
    expect(main?.segments.map((s) => [s.kind, s.to - s.from])).toEqual([
      ["work", 2 * 60_000],
      ["wait", 6 * 60_000],
      ["work", 2 * 60_000],
    ]);
    expect(main?.marks.map((m) => m.kind)).toEqual(["handoff", "return"]);
    expect(main?.work).toBe("List");
  });

  it("does not count a background sub-agent as the main agent waiting", () => {
    const main = buildLanes(events, [task({ background: true })], false, null, "run")?.lanes[0];
    expect(main?.segments.map((s) => s.kind)).toEqual(["work"]);
  });

  it("puts the sub-agents on lanes, newest first, and the background commands on one lane", () => {
    const board = buildLanes(
      events,
      [task({ id: "a1" }), task({ id: "a2", startedAt: at(5), endedAt: null, status: "running" }), task({ id: "c1", kind: "command", background: true, command: "tail -f log", name: "Watch", status: "running", startedAt: at(3), endedAt: null })],
      true,
      Date.parse(at(12)),
      "run",
    );
    expect(board?.lanes.map((l) => l.id)).toEqual(["main", "a2", "a1", "commands"]);
    expect(board?.lanes[1].status).toBe("running");
    expect(board?.lanes[3]).toMatchObject({ live: 1, count: 1, work: "tail -f log" });
    expect(board?.to).toBe(Date.parse(at(12)));
  });

  it("starts This step after the last step the main agent reported done", () => {
    const done = ev(3, 6, "tool_call", { title: "Bash", rawInput: { command: "evo-agents worker step 4 done" } });
    const all = [...events.slice(0, 1), done, ev(4, 7, "agent_message_chunk"), ev(5, 10, "tool_call")];
    expect(stepStart(all)).toBe(Date.parse(at(6)));
    expect(stepStart(events)).toBe(Date.parse(at(0)));
    const step = buildLanes(all, [task({})], false, null, "step");
    const whole = buildLanes(all, [task({})], false, null, "run");
    expect(step?.from).toBe(Date.parse(at(6)));
    expect(whole?.from).toBe(Date.parse(at(0)));
  });
});
