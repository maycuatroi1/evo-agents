import { planEntries, isRecord } from "./trace-events";
import type { RunEvent } from "./queries";
import { parentOf, type RunTask } from "./run-tasks";

/**
 * "Step N so far": what the current step has done, is doing and has left, built from what the page already holds and
 * with no model call. The sources, in order of preference: the main agent's latest checklist (its `plan` event, from
 * TodoWrite), the subagents' state and results, and the step's `done_when` from the plan. Every item says where it
 * came from; the section says when its sources last changed. Pure, shared by the page and its tests.
 */

export type SoFarSource = "checklist" | "subagent" | "done_when";

export type SoFarItem = {
  key: string;
  text: string;
  source: SoFarSource;
  /** Who is doing it: set on the items of Now, the main agent (null id) or a subagent (its task id). */
  agent: { name: string | null; id: string | null } | null;
  /** A subagent's item that ended badly. */
  failed?: boolean;
};

export type StepSoFar = {
  done: SoFarItem[];
  now: SoFarItem[];
  next: SoFarItem[];
  /** The sources the items came from, in order of preference. */
  sources: SoFarSource[];
  /** When the newest of those sources changed. */
  at: string | null;
  empty: boolean;
};

const newer = (a: string | null, b: string | null) => (a === null ? b : b === null ? a : Date.parse(b) > Date.parse(a) ? b : a);

/** The main agent's latest checklist: its entries, and when it was written. */
export function latestChecklist(events: readonly Pick<RunEvent, "kind" | "body" | "at">[]): { entries: ReturnType<typeof planEntries>; at: string } | null {
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (event.kind !== "plan" || parentOf(event) !== null || !isRecord(event.body)) continue;
    const entries = planEntries(event.body);
    if (entries.length > 0) return { entries, at: event.at };
  }
  return null;
}

export function stepSoFar({
  events,
  tasks,
  active,
  doneWhen,
}: {
  events: readonly Pick<RunEvent, "kind" | "body" | "at">[];
  tasks: readonly RunTask[];
  active: boolean;
  doneWhen: readonly string[];
}): StepSoFar {
  const done: SoFarItem[] = [];
  const now: SoFarItem[] = [];
  const next: SoFarItem[] = [];
  const sources = new Set<SoFarSource>();
  let at: string | null = null;

  const checklist = latestChecklist(events);
  if (checklist) {
    sources.add("checklist");
    at = newer(at, checklist.at);
    checklist.entries.forEach((entry, index) => {
      const item: SoFarItem = { key: `c${index}`, text: entry.content, source: "checklist", agent: null };
      if (entry.status === "completed") done.push(item);
      else if (entry.status === "in_progress") now.push({ ...item, agent: { name: null, id: null } });
      else next.push(item);
    });
  }

  for (const task of tasks) {
    if (task.kind !== "agent") continue;
    const label = task.activity ?? task.name;
    sources.add("subagent");
    if (task.status === "running" && active) {
      now.push({ key: `a${task.id}`, text: label, source: "subagent", agent: { name: task.name, id: task.id } });
      at = newer(at, task.startedAt);
    } else {
      const failed = task.status === "failed";
      done.push({ key: `a${task.id}`, text: task.summary ?? task.name, source: "subagent", agent: { name: task.name, id: task.id }, failed });
      at = newer(at, task.endedAt ?? task.startedAt);
    }
  }

  if (next.length === 0 && doneWhen.length > 0) {
    sources.add("done_when");
    doneWhen.forEach((sentence, index) => next.push({ key: `w${index}`, text: sentence, source: "done_when", agent: null }));
  }

  return { done, now, next, sources: (["checklist", "subagent", "done_when"] as const).filter((source) => sources.has(source)), at, empty: done.length + now.length + next.length === 0 };
}
