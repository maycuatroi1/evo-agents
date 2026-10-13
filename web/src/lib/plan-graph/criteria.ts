import { isObject, type PlanStep, text } from "@/lib/plans";

/**
 * The acceptance a step names, for the graph's inspector. An outcome step lists under `acceptance` the ids of plan
 * criteria (`a4`), whose text is in the plan's own `acceptance`, or sentences of its own. Pure: no React, no DOM.
 */
export type StepCriterion = {
  /** The plan criterion's id, or null for a sentence the step states itself. */
  id: string | null;
  /** The criterion as the plan words it; null when the step names an id the plan does not have. */
  text: string | null;
};

/** The plan's criteria by id, from its `acceptance` section (a list of `{id, criterion}`; `what` is read too). */
export function planCriteria(acceptance: unknown): Map<string, string> {
  const found = new Map<string, string>();
  if (!Array.isArray(acceptance)) return found;
  for (const item of acceptance) {
    if (!isObject(item)) continue;
    const id = text(item.id);
    const words = text(item.criterion) ?? text(item.what);
    if (id !== null && words !== null && !found.has(id)) found.set(id, words.trim());
  }
  return found;
}

/** The criteria `step` names under `acceptance`, in its order; an entry that is no id of the plan is a sentence. */
export function stepCriteria(step: Pick<PlanStep, "extra">, criteria: ReadonlyMap<string, string>): StepCriterion[] {
  const listed = step.extra.find(([key]) => key === "acceptance")?.[1];
  const items = Array.isArray(listed) ? listed : listed === undefined ? [] : [listed];
  return items
    .map(text)
    .filter((item): item is string => item !== null && item.trim() !== "")
    .map((item) => {
      const id = item.trim();
      if (criteria.has(id)) return { id, text: criteria.get(id) ?? null };
      // A short token with no space reads as an id the plan lacks; anything longer is a sentence of the step's own.
      return /^\S{1,24}$/.test(id) ? { id, text: null } : { id: null, text: item.trim() };
    });
}
