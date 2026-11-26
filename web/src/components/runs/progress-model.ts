import type { PlanStep } from "@/lib/plans";

import type { RunEvent } from "./queries";
import { parentOf, type RunTask } from "./run-tasks";

/**
 * What the Progress view of a run says, read from what the page already holds: the plan (its steps, evidence, done_when)
 * and the run's events and tasks. No request and no model. Pure, shared by the view and its tests.
 *
 * The hub keeps no record of where a step began and ended inside a run's log, so the boundaries are read from the one
 * trace a step leaves: the main agent's `evo-agents worker step <id> done ...` command. A step ends at that command and
 * begins where the one before it ended (the first at the run's first event). A step the run did not report has none.
 */

const STEP_DONE = /\bworker\s+step\s+["']?([^\s"']+)["']?\s+done\b/;

/** Where a step of this run ended: the main agent's `worker step <key> done` call. */
export type Boundary = { key: string; seq: number; at: number; call: string | null };

/** The stretch of the log a step took; `open` while it is the step the run works on. */
export type StepSpan = { key: string; from: number; to: number; open: boolean; call: string | null };

const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : Number.NaN);

function commandOf(event: Pick<RunEvent, "body">): string | null {
  const input = event.body.rawInput;
  if (typeof input !== "object" || input === null) return null;
  const command = (input as Record<string, unknown>).command;
  return typeof command === "string" ? command : null;
}

/** The `worker step N done` calls of the main agent, one per step (the last one when it ran it twice), in seq order. */
export function stepBoundaries(events: readonly RunEvent[]): Boundary[] {
  const found = new Map<string, Boundary>();
  for (const event of events) {
    if (event.kind !== "tool_call" || parentOf(event) !== null) continue;
    const match = STEP_DONE.exec(commandOf(event) ?? "");
    const at = ms(event.at);
    if (!match || Number.isNaN(at)) continue;
    const call = typeof event.body.toolCallId === "string" ? event.body.toolCallId : typeof event.body.id === "string" ? event.body.id : null;
    const previous = found.get(match[1]);
    if (!previous || event.seq > previous.seq) found.set(match[1], { key: match[1], seq: event.seq, at, call });
  }
  return [...found.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * The span of each step the run reported, and of the step it works on (`running`, null when none) up to `until`. The
 * first step starts at the run's first event.
 */
export function stepSpans(events: readonly RunEvent[], running: string | null, until: number | null): StepSpan[] {
  const times = events.map((event) => ms(event.at)).filter((time) => !Number.isNaN(time));
  if (times.length === 0) return [];
  const first = Math.min(...times);
  const last = Math.max(...times);
  const boundaries = stepBoundaries(events);
  const spans: StepSpan[] = [];
  let cursor = first;
  for (const boundary of boundaries) {
    spans.push({ key: boundary.key, from: cursor, to: Math.max(boundary.at, cursor), open: false, call: boundary.call });
    cursor = Math.max(boundary.at, cursor);
  }
  if (running !== null && !boundaries.some((boundary) => boundary.key === running)) {
    spans.push({ key: running, from: cursor, to: Math.max(until ?? last, last, cursor), open: true, call: null });
  }
  return spans;
}

export type EvidenceCommit = { repo: string | null; sha: string };

export type Evidence = {
  commits: EvidenceCommit[];
  /** The line a step's evidence gives as its verify, without the label. */
  verify: string | null;
  /** Whether that line says it passed (`exit 0`, `pass`), failed, or neither. */
  passed: boolean | null;
};

const COMMIT = /\b([A-Za-z0-9._-]+)@([0-9a-f]{7,40})\b/g;
const VERIFY = /^\s*verify[^:\n]*:\s*(.+)$/im;

/** The commits and the verify a step's evidence names; the evidence is free text, so what it does not name is null. */
export function readEvidence(text: string | null | undefined): Evidence {
  const source = text ?? "";
  const commits: EvidenceCommit[] = [];
  for (const [, repo, sha] of source.matchAll(COMMIT)) {
    if (commits.some((known) => known.sha.startsWith(sha) || sha.startsWith(known.sha))) continue;
    commits.push({ repo, sha });
  }
  const verify = VERIFY.exec(source)?.[1]?.trim() ?? null;
  const passed = verify === null ? null : /\bexit\s+0\b|\bpass(?:ed|es)?\b/i.test(verify) ? true : /\bexit\s+[1-9]|\bfail/i.test(verify) ? false : null;
  return { commits, verify, passed };
}

/** The sub-agents that took part in a step: the ones that started inside its span. */
export function agentsIn(tasks: readonly RunTask[], span: Pick<StepSpan, "from" | "to"> | null): RunTask[] {
  if (!span) return [];
  return tasks.filter((task) => {
    if (task.kind !== "agent") return false;
    const start = ms(task.startedAt);
    return start >= span.from && start <= span.to;
  });
}

export type StepCard = {
  step: PlanStep;
  /** The step the run works on now. */
  running: boolean;
  span: StepSpan | null;
  evidence: Evidence;
  agents: RunTask[];
};

export type ProgressBoard = {
  /** Newest first: the running step, then the steps with something to show. */
  cards: StepCard[];
  /** Steps nobody started, in plan order. */
  pending: PlanStep[];
  total: number;
  done: number;
};

const doneTime = (step: PlanStep) => ms(step.doneAt);

/**
 * The cards of the Progress view, newest first: the running step on top, then the others by when they finished (by their
 * place in the plan when a plan gives no time). A pending step that is not the running one goes to `pending`.
 */
export function progressBoard({
  steps,
  running,
  spans,
  tasks,
}: {
  steps: readonly PlanStep[];
  running: string | null;
  spans: readonly StepSpan[];
  tasks: readonly RunTask[];
}): ProgressBoard {
  const cards: StepCard[] = [];
  const pending: PlanStep[] = [];
  for (const step of steps) {
    const isRunning = step.key === running && step.group !== "done";
    if (step.group === "pending" && !isRunning) {
      pending.push(step);
      continue;
    }
    const span = spans.find((candidate) => candidate.key === step.key) ?? null;
    cards.push({ step, running: isRunning, span, evidence: readEvidence(step.evidence), agents: agentsIn(tasks, span) });
  }
  cards.sort((a, b) => {
    if (a.running !== b.running) return a.running ? -1 : 1;
    const [x, y] = [doneTime(a.step), doneTime(b.step)];
    if (!Number.isNaN(x) && !Number.isNaN(y) && x !== y) return y - x;
    return b.step.index - a.step.index;
  });
  return { cards, pending, total: steps.length, done: steps.filter((step) => step.group === "done").length };
}

export type RunSoFar = {
  total: number;
  done: number;
  current: { key: string; title: string | null } | null;
  commits: number;
  agents: number;
  agentsRunning: number;
  agentsFailed: number;
};

/** The figures of the "Run so far" paragraph, from the board and the tasks. */
export function runSoFar(board: ProgressBoard, tasks: readonly RunTask[], running: boolean): RunSoFar {
  const current = board.cards.find((card) => card.running)?.step ?? null;
  const subagents = tasks.filter((task) => task.kind === "agent");
  const shas = new Set<string>();
  for (const card of board.cards) for (const commit of card.evidence.commits) shas.add(commit.sha.slice(0, 7));
  return {
    total: board.total,
    done: board.done,
    current: current ? { key: current.key, title: current.title } : null,
    commits: shas.size,
    agents: subagents.length,
    agentsRunning: running ? subagents.filter((task) => task.status === "running").length : 0,
    agentsFailed: subagents.filter((task) => task.status === "failed").length,
  };
}
