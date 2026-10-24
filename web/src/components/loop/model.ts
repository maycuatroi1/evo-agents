import {
  Ban,
  Binoculars,
  CircleCheck,
  ClipboardCheck,
  CircleDot,
  CircleMinus,
  CirclePause,
  CirclePlay,
  Clock,
  FilePen,
  type LucideIcon,
  MessageSquare,
  Route,
  ScanSearch,
  Telescope,
  TimerOff,
  Truck,
} from "lucide-react";

import type { StatusLook } from "@/components/status/status-badge";
import {
  type BoardGroup,
  type BoardItem,
  type GoalMetric,
  ITEM_KINDS,
  ITEM_STATES,
  type ItemKind,
  type ItemsFilter,
  type ItemState,
  type LoopGoal,
  type LoopItem,
  type ProjectGoal,
} from "@/lib/loop-queries";

/**
 * The words and shapes of the Loop's pages (plan loop-engine): how a work item's state, kind and goal look, the
 * filters of the Loop page in its address, the chains of a list of work items and how near a goal is to its target.
 * Pure: no React, no query.
 */

/** A work item's state as a pill: running is an agent at work, waiting_human a person needed, done and cancelled ended. */
export const STATE_LOOKS: Record<ItemState, StatusLook> = {
  queued: { tone: "neutral", icon: Clock },
  running: { tone: "running", icon: CirclePlay, dot: "live" },
  watching: { tone: "neutral", icon: Binoculars },
  waiting_human: { tone: "attention", icon: MessageSquare },
  done: { tone: "success", icon: CircleCheck },
  cancelled: { tone: "outline", icon: Ban },
};

/** One icon per kind of work item (web/DESIGN.md, Iconography). */
export const KIND_ICONS: Record<ItemKind, LucideIcon> = {
  brainstorm: Telescope,
  discover: ScanSearch,
  use_case: Route,
  build: FilePen,
  deliver: Truck,
  act: ClipboardCheck,
};

/** A goal's status as a pill; a closed goal by why it is closed. */
export type GoalWord = "active" | "paused" | "achieved" | "dropped" | "expired";

export const GOAL_LOOKS: Record<GoalWord, StatusLook> = {
  active: { tone: "neutral", icon: CircleDot },
  paused: { tone: "outline", icon: CirclePause },
  achieved: { tone: "success", icon: CircleCheck },
  dropped: { tone: "outline", icon: CircleMinus },
  expired: { tone: "neutral", icon: TimerOff },
};

export function goalWord(goal: Pick<ProjectGoal, "status" | "close_reason">): GoalWord {
  if (goal.status === "closed") return goal.close_reason ?? "dropped";
  return goal.status;
}

/** The order the Loop page lists states in: agents at work and people needed first, what ended last. */
const STATE_ORDER: readonly ItemState[] = ["running", "waiting_human", "queued", "watching", "done", "cancelled"];

export function isItemState(value: string | null): value is ItemState {
  return value !== null && (ITEM_STATES as readonly string[]).includes(value);
}

export function isItemKind(value: string | null): value is ItemKind {
  return value !== null && (ITEM_KINDS as readonly string[]).includes(value);
}

/** A goal's code or alias, as `goals.REF` takes it. */
const GOAL_REF = /^(?:G-[1-9][0-9]{0,8}|[a-z0-9][a-z0-9-]{0,63})$/;

export type LoopView = "state" | "chain";

export type LoopFilters = ItemsFilter & { view: LoopView };

export const NO_LOOP_FILTERS: LoopFilters = { state: null, kind: null, goal: null, open: false, view: "state" };

type Params = { get: (name: string) => string | null };

/** The Loop page's filters as its address holds them (`?state=&kind=&goal=&view=chain`); a value it does not know reads as none. */
export function readLoopFilters(params: Params): LoopFilters {
  const state = params.get("state");
  const kind = params.get("kind");
  const goal = params.get("goal");
  return {
    state: isItemState(state) ? state : null,
    kind: isItemKind(kind) ? kind : null,
    goal: goal && GOAL_REF.test(goal) ? goal : null,
    open: false,
    view: params.get("view") === "chain" ? "chain" : "state",
  };
}

export function loopSearch(filters: LoopFilters): string {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.goal) params.set("goal", filters.goal);
  if (filters.view === "chain") params.set("view", "chain");
  return params.toString();
}

/** The read the page makes of its filters: the view does not change what is read. */
export function itemsFilterOf(filters: LoopFilters): ItemsFilter {
  return { state: filters.state, kind: filters.kind, goal: filters.goal, open: filters.open };
}

/** Work items by state in the page's order, the newest change first within a state. */
export function byState(items: readonly LoopItem[]): LoopItem[] {
  return [...items].sort(
    (a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state) || Date.parse(b.updated_at) - Date.parse(a.updated_at) || b.id - a.id,
  );
}

export type ChainRow = { item: LoopItem; depth: number };
export type Chain = { root: number; rows: ChainRow[]; updated: number };

/**
 * The listed work items by their chain (`root`): each chain's work items after the one each came of, its newest
 * change first among the chains. A work item whose parent is not listed starts its own line in the chain.
 */
export function chainsOf(items: readonly LoopItem[]): Chain[] {
  const byRoot = new Map<number, LoopItem[]>();
  for (const item of items) byRoot.set(item.root, [...(byRoot.get(item.root) ?? []), item]);
  const chains: Chain[] = [];
  for (const [root, members] of byRoot) {
    const ids = new Set(members.map((item) => item.id));
    const children = new Map<number | null, LoopItem[]>();
    for (const item of [...members].sort((a, b) => a.id - b.id)) {
      const parent = item.parent !== null && ids.has(item.parent) ? item.parent : null;
      children.set(parent, [...(children.get(parent) ?? []), item]);
    }
    const rows: ChainRow[] = [];
    const walk = (parent: number | null, depth: number) => {
      for (const item of children.get(parent) ?? []) {
        rows.push({ item, depth });
        walk(item.id, depth + 1);
      }
    };
    walk(null, 0);
    chains.push({ root, rows, updated: Math.max(...members.map((item) => Date.parse(item.updated_at))) });
  }
  return chains.sort((a, b) => b.updated - a.updated || b.root - a.root);
}

/**
 * What a work item is about, as its title names it: the goal of a brainstorm, the claim of a discover (a problem's claim
 * whose test card it runs), of an act (a task that delivers a solution without code) and of a use case, the use case of a build, the plan of a deliver.
 */
export function subjectOf(item: Pick<LoopItem, "kind" | "goal" | "claim" | "use_case" | "plan">): string | null {
  switch (item.kind) {
    case "brainstorm":
      return item.goal;
    case "discover":
    case "act":
      return item.claim?.code ?? item.goal;
    case "use_case":
      return item.claim?.code ?? item.use_case?.document ?? null;
    case "build":
      return item.use_case?.code ?? item.claim?.code ?? null;
    case "deliver":
      return item.plan ?? item.use_case?.code ?? null;
  }
}

/** How a metric reads: `checkout.conversion mean over 30 days >= 0.4`, as `goals.metric_text` writes it. */
export function metricParts(metric: GoalMetric): { key: string; aggregate: string; days: number; comparator: string; target: number; tenant: string | null } {
  return {
    key: metric.key,
    aggregate: metric.aggregate,
    days: metric.window_days ?? 90,
    comparator: metric.comparator,
    target: metric.target,
    tenant: metric.tenant ?? null,
  };
}

/**
 * How near a goal's value is to its target, 0 to 100, for its meter: the share of the target reached for a goal that
 * wants more (>=, >), and of the value the target allows for one that wants less (<=, <); null without a value or
 * with a target the share cannot be taken of.
 */
export function progressOf(metric: GoalMetric | null, figure: LoopGoal | null): number | null {
  if (!metric || !figure || figure.value === null) return null;
  const { value } = figure;
  const { target } = metric;
  if (figure.reached) return 100;
  if (metric.comparator === ">=" || metric.comparator === ">") {
    return target > 0 && value >= 0 ? Math.min(100, (value / target) * 100) : null;
  }
  return value > 0 && target >= 0 ? Math.min(100, (target / value) * 100) : null;
}

/** Work items by state, as the hub counts them: a state it has none of may be missing. */
export type ItemCounts = Readonly<Record<string, number>>;

/** How many work items are in `state`. */
export function countOf(items: ItemCounts | null | undefined, state: ItemState): number {
  return items?.[state] ?? 0;
}

/** The unfinished work items: what the Loop still does. */
export function openCount(items: ItemCounts): number {
  return countOf(items, "queued") + countOf(items, "running") + countOf(items, "watching") + countOf(items, "waiting_human");
}

/** Every work item counted. */
export function totalCount(items: ItemCounts): number {
  return ITEM_STATES.reduce((sum, state) => sum + countOf(items, state), 0);
}

/** The board's items by group, in the board's order. */
export function boardGroups(items: readonly BoardItem[]): Map<BoardGroup, BoardItem[]> {
  const groups = new Map<BoardGroup, BoardItem[]>();
  for (const item of items) groups.set(item.group, [...(groups.get(item.group) ?? []), item]);
  return groups;
}
