import { describe, expect, it } from "vitest";

import type { PlanStep } from "@/lib/plans";

import { buildLanes } from "./agent-lanes-model";
import { progressBoard, readEvidence, runSoFar, stepBoundaries, stepSpans } from "./progress-model";
import type { RunTask } from "./run-tasks";

const at = (minute: number) => `2026-11-26T10:${String(minute).padStart(2, "0")}:00.000Z`;
const ms = (minute: number) => Date.parse(at(minute));
const bash = (seq: number, minute: number, command: string, over: Record<string, unknown> = {}) => ({
  seq,
  at: at(minute),
  kind: "tool_call" as const,
  body: { toolCallId: `call-${seq}`, rawInput: { command }, ...over },
});
const step = (key: string, over: Partial<PlanStep> = {}): PlanStep =>
  ({ key, index: Number(key), title: `Title ${key}`, repo: null, what: null, verify: null, note: null, evidence: null, doneAt: null, group: "pending", rawStatus: null, blocking: null, dependsOn: [], extra: [], ...over }) as PlanStep;
const agent = (id: string, startedAt: string, over: Partial<RunTask> = {}): RunTask =>
  ({ id, kind: "agent", type: "local_agent", name: `Agent ${id}`, agentType: null, parent: null, background: false, callId: null, status: "completed", startedAt, endedAt: null, activity: null, lastTool: null, toolUses: null, tokens: null, summary: null, prompt: null, result: null, command: null, firstSeq: 1, seq: 1, ...over }) as RunTask;

describe("progress view", () => {
  const events = [
    bash(1, 0, "ls"),
    bash(2, 10, "evo-agents worker step 1 done --evidence x"),
    bash(3, 12, "ls"),
    bash(4, 20, "cd repo && evo-agents worker step 2 done"),
    bash(5, 21, "evo-agents worker step 3 done", { _meta: { parentToolCallId: "toolu_sub" } }),
  ] as never;

  it("progress view: ends a step at the main agent's `worker step N done` and starts it where the one before ended", () => {
    expect(stepBoundaries(events).map((boundary) => [boundary.key, boundary.call])).toEqual([["1", "call-2"], ["2", "call-4"]]);
    const spans = stepSpans(events, "3", ms(30));
    expect(spans.map((span) => [span.key, span.from, span.to, span.open])).toEqual([
      ["1", ms(0), ms(10), false],
      ["2", ms(10), ms(20), false],
      ["3", ms(20), ms(30), true],
    ]);
  });

  it("progress view: reads commits and the verify from a step's evidence, as free text", () => {
    const evidence = readEvidence("Evidence: run #1 on worker w: evo-agents@434e8ab55b84 on feat/x.\nverify: `cd web && pnpm lint` exit 0.\nevo-agents@71da239c (merged): the card.\nevo-agents@71da239c2222 again");
    expect(evidence.commits.map((commit) => commit.sha)).toEqual(["434e8ab55b84", "71da239c"]);
    expect(evidence.verify).toBe("`cd web && pnpm lint` exit 0.");
    expect(evidence.passed).toBe(true);
    expect(readEvidence("verify: pnpm test exit 1").passed).toBe(false);
    expect(readEvidence(null)).toEqual({ commits: [], verify: null, passed: null });
  });

  it("progress view: lists the running step first, then the others newest first, and folds the steps not started", () => {
    const steps = [
      step("1", { group: "done", doneAt: at(10), evidence: "api@abc1234: pass" }),
      step("2", { group: "done", doneAt: at(20) }),
      step("3", { group: "in_progress" }),
      step("4"),
      step("5"),
    ];
    const tasks = [agent("a", at(5)), agent("b", at(25), { status: "running" })];
    const board = progressBoard({ steps, running: "3", spans: stepSpans(events, "3", ms(30)), tasks });
    expect(board.cards.map((card) => card.step.key)).toEqual(["3", "2", "1"]);
    expect(board.pending.map((pending) => pending.key)).toEqual(["4", "5"]);
    expect(board.cards[2].agents.map((found) => found.id)).toEqual(["a"]);
    expect(board.cards[0].agents.map((found) => found.id)).toEqual(["b"]);
    expect(board.cards[2].evidence.commits).toHaveLength(1);
    const so = runSoFar(board, tasks, true);
    expect(so).toMatchObject({ total: 5, done: 2, current: { key: "3" }, commits: 1, agents: 2, agentsRunning: 1 });
  });

  it("progress view: marks each reported step on the main agent's lane over the whole run, even with no sub-agent", () => {
    const marks = stepBoundaries(events).map((boundary) => ({ key: boundary.key, at: boundary.at }));
    expect(buildLanes(events, [], false, null, "run")).toBeNull();
    const board = buildLanes(events, [], false, null, "run", { steps: marks });
    expect(board?.lanes.map((lane) => lane.kind)).toEqual(["main"]);
    expect(board?.lanes[0].marks.map((mark) => [mark.kind, mark.agent])).toEqual([["step", "1"], ["step", "2"]]);
  });
});
