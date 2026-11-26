import { describe, expect, it } from "vitest";

import type { RunTask } from "./run-tasks";
import { criteriaOf, doneWhenOf } from "./step-criteria";
import { latestChecklist, stepSoFar } from "./step-so-far";

const at = (minute: number) => `2026-11-26T10:${String(minute).padStart(2, "0")}:00.000Z`;
const checklist = (minute: number, entries: [string, string][], parent?: string) => ({
  kind: "plan" as const,
  at: at(minute),
  body: { entries: entries.map(([content, status]) => ({ content, status })), ...(parent ? { _meta: { parentToolCallId: parent } } : {}) },
});
const task = (id: string, over: Partial<RunTask> = {}): RunTask =>
  ({ id, kind: "agent", type: "local_agent", name: `Agent ${id}`, agentType: null, parent: null, background: false, callId: null, status: "running", startedAt: at(5), endedAt: null, activity: null, lastTool: null, toolUses: null, tokens: null, summary: null, prompt: null, result: null, command: null, firstSeq: 1, seq: 1, ...over }) as RunTask;

describe("step so far", () => {
  it("so far: takes Done, Now and Next from the agent's latest checklist, and says where each came from", () => {
    const events = [
      checklist(1, [["old", "pending"]]),
      checklist(7, [["Read the plan", "completed"], ["Write the model", "in_progress"], ["Run the tests", "pending"]]),
      checklist(9, [["a subagent's own list", "pending"]], "toolu_1"),
    ];
    const so = stepSoFar({ events: events as never, tasks: [], active: true, doneWhen: ["tests pass"] });
    expect(so.done.map((item) => item.text)).toEqual(["Read the plan"]);
    expect(so.now.map((item) => [item.text, item.agent])).toEqual([["Write the model", { name: null, id: null }]]);
    expect(so.next.map((item) => [item.text, item.source])).toEqual([["Run the tests", "checklist"]]);
    expect(so.sources).toEqual(["checklist"]);
    expect(so.at).toBe(at(7));
  });

  it("so far: adds the subagents, running ones to Now and finished ones to Done with their report", () => {
    const so = stepSoFar({
      events: [],
      tasks: [task("a", { activity: "Running the tests" }), task("b", { status: "completed", endedAt: at(8), summary: "Added the card" }), task("c", { kind: "command" })],
      active: true,
      doneWhen: [],
    });
    expect(so.now.map((item) => [item.text, item.agent?.id])).toEqual([["Running the tests", "a"]]);
    expect(so.done.map((item) => [item.text, item.source])).toEqual([["Added the card", "subagent"]]);
    expect(so.at).toBe(at(8));
  });

  it("so far: falls back to the step's done_when for Next, and says there is nothing when no source has anything", () => {
    expect(stepSoFar({ events: [], tasks: [], active: true, doneWhen: ["the page shows it"] }).next).toMatchObject([{ text: "the page shows it", source: "done_when" }]);
    const none = stepSoFar({ events: [], tasks: [], active: true, doneWhen: [] });
    expect(none.empty).toBe(true);
    expect(none.sources).toEqual([]);
    expect(latestChecklist([])).toBeNull();
  });

  it("so far: reads a step's done_when and the criteria it names, met or not", () => {
    const step = { group: "in_progress" as const, extra: [["acceptance", ["a1", "a2", "ghost"]], ["done_when", ["one", "two"]]] as [string, unknown][] };
    const view = { sections: [["acceptance", [{ id: "a1", criterion: "It works", status: "met" }, { id: "a2", criterion: "It is fast", status: "pending" }]]] as [string, unknown][] };
    expect(doneWhenOf(step)).toEqual(["one", "two"]);
    expect(criteriaOf(view, step)).toEqual([
      { id: "a1", text: "It works", met: true },
      { id: "a2", text: "It is fast", met: false },
      { id: null, text: "ghost", met: false },
    ]);
  });
});
