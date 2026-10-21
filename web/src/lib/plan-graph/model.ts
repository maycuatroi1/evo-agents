import type { PlanStep } from "@/lib/plans";

/**
 * The dependencies of a plan's steps as a graph, before any of it is placed: which depends_on entries name a step of
 * the plan, which close a cycle, which are implied by another path and so not drawn, and what each step waits for.
 * Pure: no React, no DOM, nothing changed in the steps it reads.
 */

/**
 * Why a dependency is or is not drawn: `drawn`, a line on the graph; `implied`, reachable through another dependency
 * of the same step (1 to 6 when 6 also waits for 5, which waits for 1); `unknown`, depends_on names no step of the
 * plan; `cycle`, the entry closes a cycle in a hand-written plan (a step waiting for itself included).
 */
export type DependencyKind = "drawn" | "implied" | "unknown" | "cycle";

export type Dependency = {
  /** The key depends_on names: the step waited for. */
  from: string;
  /** The key of the step that waits. */
  to: string;
  /** Plan positions (PlanStep.index); `fromIndex` is null for a key no step has. */
  fromIndex: number | null;
  toIndex: number;
  kind: DependencyKind;
  /** Whether the step waited for is done: a line from a step not done is drawn dashed. */
  fromDone: boolean;
};

export type StepRelations = {
  /** Pending, and every dependency that names a step of the plan is done. */
  ready: boolean;
  /** The keys of the steps it depends on that are not done, in depends_on order (unknown keys left out). */
  waitingOn: string[];
  /** Every step it waits for, directly or not, in plan order. */
  ancestors: string[];
  /** Every step that waits for it, directly or not, in plan order. */
  descendants: string[];
};

export type DependencyGraph = {
  /** Every depends_on entry once, ordered by the waiting step's plan position, then as depends_on lists them. */
  dependencies: Dependency[];
  /** Per step, in plan order (aligned with the steps passed in). */
  relations: StepRelations[];
  /** Per step, the positions of the steps it hangs under on the graph (drawn dependencies only). */
  parents: number[][];
};

/** A key names the first step that has it, as evo-cli's step lookup does; later duplicates are never a target. */
export function keyIndex(steps: readonly PlanStep[]): Map<string, number> {
  const found = new Map<string, number>();
  steps.forEach((step, position) => {
    if (!found.has(step.key)) found.set(step.key, position);
  });
  return found;
}

/** Positions reachable from `start` through `next`, without `start` itself unless a cycle leads back to it. */
function reach(start: number, next: readonly number[][]): Set<number> {
  const seen = new Set<number>();
  const stack = [...next[start]];
  while (stack.length) {
    const at = stack.pop() as number;
    if (seen.has(at)) continue;
    seen.add(at);
    stack.push(...next[at]);
  }
  return seen;
}

/** Positions in plan order, as keys. */
function asKeys(steps: readonly PlanStep[], positions: Set<number>): string[] {
  return [...positions].filter((p) => p >= 0).sort((a, b) => a - b).map((p) => steps[p].key);
}

export function dependencyGraph(steps: readonly PlanStep[]): DependencyGraph {
  const byKey = keyIndex(steps);
  // depends_on per step, each key once and in its order, resolved to a position (-1 for a key no step has).
  const declared = steps.map((step) => [...new Set(step.dependsOn)].map((key) => ({ key, at: byKey.get(key) ?? -1 })));

  // Cycles: a depth-first walk along depends_on in plan order; an entry that leads back to a step still on the walk
  // closes a cycle. Iterative, so a long hand-written chain cannot overflow the stack.
  const cycle = steps.map(() => new Set<string>());
  const state = steps.map(() => 0); // 0 not seen, 1 on the walk, 2 finished
  for (let root = 0; root < steps.length; root += 1) {
    if (state[root] !== 0) continue;
    const walk: { at: number; next: number }[] = [{ at: root, next: 0 }];
    state[root] = 1;
    while (walk.length) {
      const frame = walk[walk.length - 1];
      const entry = declared[frame.at][frame.next];
      if (!entry) {
        state[frame.at] = 2;
        walk.pop();
        continue;
      }
      frame.next += 1;
      if (entry.at < 0) continue;
      if (state[entry.at] === 1) cycle[frame.at].add(entry.key);
      else if (state[entry.at] === 0) {
        state[entry.at] = 1;
        walk.push({ at: entry.at, next: 0 });
      }
    }
  }

  // What each step waits for along the entries that close no cycle: the plan's shape as a DAG.
  const acyclic = declared.map((entries, at) =>
    entries.filter((entry) => entry.at >= 0 && !cycle[at].has(entry.key)).map((entry) => entry.at),
  );
  const above = acyclic.map((_, at) => reach(at, acyclic));

  const dependencies: Dependency[] = [];
  const parents: number[][] = steps.map(() => []);
  declared.forEach((entries, at) => {
    const direct = acyclic[at];
    for (const entry of entries) {
      let kind: DependencyKind;
      if (entry.at < 0) kind = "unknown";
      else if (cycle[at].has(entry.key)) kind = "cycle";
      else if (direct.some((other) => other !== entry.at && above[other].has(entry.at))) kind = "implied";
      else kind = "drawn";
      if (kind === "drawn") parents[at].push(entry.at);
      dependencies.push({
        from: entry.key,
        to: steps[at].key,
        fromIndex: entry.at < 0 ? null : steps[entry.at].index,
        toIndex: steps[at].index,
        kind,
        fromDone: entry.at >= 0 && steps[entry.at].group === "done",
      });
    }
  });

  // Ancestors and descendants follow every entry that names a step, cycles included, so a selection in a cycle
  // keeps the whole cycle lit.
  const up = declared.map((entries) => entries.filter((entry) => entry.at >= 0).map((entry) => entry.at));
  const down: number[][] = steps.map(() => []);
  up.forEach((targets, at) => targets.forEach((target) => down[target].push(at)));

  const relations = steps.map((step, at): StepRelations => {
    const known = declared[at].filter((entry) => entry.at >= 0);
    const waitingOn = known.filter((entry) => steps[entry.at].group !== "done").map((entry) => entry.key);
    const ancestors = reach(at, up);
    const descendants = reach(at, down);
    ancestors.delete(at);
    descendants.delete(at);
    return {
      ready: step.group === "pending" && waitingOn.length === 0,
      waitingOn,
      ancestors: asKeys(steps, ancestors),
      descendants: asKeys(steps, descendants),
    };
  });

  return { dependencies, relations, parents };
}
