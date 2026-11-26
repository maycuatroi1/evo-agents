import { isObject, type PlanStep, type PlanView, text } from "@/lib/plans";

/**
 * What a plan step must show to be done, as the plan states it: the step's `done_when` (a sentence or a list of them)
 * and the criteria its `acceptance` names, each looked up in the plan's `acceptance` section by id, with whether the
 * plan says it is met. Pure, read from the plan the page already holds (no request, no model).
 */

export type StepCriterion = { id: string | null; text: string; met: boolean };

const MET = new Set(["met", "done", "passed", "pass", "ok", "verified", "true"]);

function lines(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(lines);
  const one = text(value);
  return one && one.trim() ? [one.trim()] : [];
}

/** The step's `done_when`, one sentence a line. */
export function doneWhenOf(step: Pick<PlanStep, "extra">): string[] {
  const found = step.extra.find(([key]) => key === "done_when");
  return found ? lines(found[1]) : [];
}

/** The criteria of the plan's `acceptance` the step names, in the order it names them; a name the plan lacks is its own text, not met. */
export function criteriaOf(view: Pick<PlanView, "sections"> | null, step: Pick<PlanStep, "extra" | "group">): StepCriterion[] {
  const named = lines(step.extra.find(([key]) => key === "acceptance")?.[1]);
  if (named.length === 0) return [];
  const table = new Map<string, StepCriterion>();
  const section = view?.sections.find(([key]) => key === "acceptance")?.[1];
  for (const entry of Array.isArray(section) ? section : []) {
    if (!isObject(entry)) continue;
    const id = text(entry.id) ?? text(entry.criterion);
    const sentence = text(entry.id) !== null ? (text(entry.criterion) ?? text(entry.what)) : (text(entry.what) ?? text(entry.criterion));
    if (id === null || sentence === null) continue;
    const status = (text(entry.status) ?? "").toLowerCase();
    table.set(id, { id, text: sentence, met: MET.has(status) || entry.met === true });
  }
  return named.map((name) => table.get(name) ?? { id: null, text: name, met: false });
}
