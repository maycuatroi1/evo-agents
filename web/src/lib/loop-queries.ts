import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { writePolicy } from "@/lib/merge-queue";
import type { ApiSource } from "@/lib/queries";

/**
 * What the pages of a project's Loop read (plan loop-engine; `evo_agents/hub/server/loop/routes.py`, `goals.py`): the
 * Loop's state, its work items and the chain of one, the goals with their metric measured now, and the decision board.
 * Any member with a grant reads them. Only an admin of the project turns the Loop on or off (a revision of the autonomy
 * policy's `loop.enabled`) or resets its breaker. The route pages prefetch them on the server, so they live here.
 */
type Schemas = components["schemas"];
export type ProjectLoop = Schemas["ProjectLoop"];
export type LoopItem = Schemas["LoopItem"];
export type LoopItems = Schemas["LoopItems"];
export type LoopItemChain = Schemas["LoopItemChain"];
export type LoopChainStages = Schemas["LoopChainStages"];
export type LoopItemEvent = Schemas["LoopItemEvent"];
export type LoopGoal = Schemas["LoopGoal"];
export type ProjectGoal = Schemas["ProjectGoal"];
export type GoalMetric = Schemas["GoalMetric"];
export type DecisionBoard = Schemas["DecisionBoard"];
export type BoardItem = Schemas["BoardItem"];
export type SrsBaseline = Schemas["SrsBaseline"];
export type BaselineItem = Schemas["BaselineItem"];

export type ItemState = LoopItem["state"];
export type ItemKind = LoopItem["kind"];
export type ItemReason = NonNullable<LoopItem["reason"]>;
export type GoalStatus = ProjectGoal["status"];
export type BoardGroup = BoardItem["group"];
export type BoardKind = BoardItem["kind"];

/** `loop.model.STATES`, in the order a work item moves through them. */
export const ITEM_STATES = ["queued", "running", "watching", "waiting_human", "done", "cancelled"] as const satisfies readonly ItemState[];
/**
 * `loop.model.KINDS`, in the order of a chain: the business cycle (a brainstorm toward a goal, a discover of a problem's
 * claim, a use case of a solution's), then the development cycle.
 */
export const ITEM_KINDS = ["brainstorm", "discover", "use_case", "build", "deliver", "act"] as const satisfies readonly ItemKind[];
/** `board_items.GROUPS`: the three gates, then what the Loop's chain waits on. */
export const BOARD_GROUPS = ["goal", "canvas", "policy", "exception", "srs"] as const satisfies readonly BoardGroup[];
/** `items.DEFAULT_ITEMS`: the newest work items one read of the Loop page lists. */
export const LOOP_ITEMS_LIMIT = 200;
/** Work items move without anyone on the page: the Loop's pages ask the hub this often. */
export const LOOP_REFRESH_MS = 15_000;

export function loopHref(project: string, rest = ""): Route {
  return `/p/${encodeURIComponent(project)}/loop${rest}` as Route;
}

export function itemHref(project: string, id: number): Route {
  return loopHref(project, `/items/${id}`);
}

export function goalsHref(project: string, code?: string): Route {
  const base = `/p/${encodeURIComponent(project)}/goals`;
  return (code ? `${base}/${encodeURIComponent(code)}` : base) as Route;
}

export function decideHref(project: string): Route {
  return `/p/${encodeURIComponent(project)}/decide` as Route;
}

export type ItemsFilter = {
  state: ItemState | null;
  kind: ItemKind | null;
  goal: string | null;
  /** Only the work items not done or cancelled. */
  open: boolean;
  /** Only the work items that go from this problem (P-<number>): a problem's page. */
  problem?: string | null;
  /** Only the work items that go from this opportunity (O-<number>). */
  opportunity?: string | null;
};

export const loopKeys = {
  /** Every read of a project's Loop: invalidated after the Loop is turned on or off, or its breaker is reset. */
  all: (project: string) => ["projects", project, "loop"] as const,
  state: (project: string) => ["projects", project, "loop", "state"] as const,
  items: (project: string, filter: ItemsFilter) => ["projects", project, "loop", "items", filter] as const,
  item: (project: string, id: number) => ["projects", project, "loop", "item", id] as const,
  goals: (project: string) => ["projects", project, "loop", "goals"] as const,
  goalList: (project: string) => ["projects", project, "goals"] as const,
  goal: (project: string, ref: string) => ["projects", project, "goals", ref] as const,
  board: (project: string) => ["projects", project, "loop", "board"] as const,
  baseline: (project: string, ref: string) => ["projects", project, "goals", ref, "baseline"] as const,
};

/** The Loop now: on or off, its breaker, its work items by state. */
export const loopQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: loopKeys.state(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/loop", { params: { path: { project } }, signal })),
  });

/** The newest LOOP_ITEMS_LIMIT work items matching `filter`. */
export const loopItemsQuery = (api: ApiSource, project: string, filter: ItemsFilter) =>
  queryOptions({
    queryKey: loopKeys.items(project, filter),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/loop/items", {
          params: {
            path: { project },
            query: {
              limit: LOOP_ITEMS_LIMIT,
              ...(filter.state ? { state: filter.state } : {}),
              ...(filter.kind ? { kind: filter.kind } : {}),
              ...(filter.goal ? { goal: filter.goal } : {}),
              ...(filter.open ? { open: true } : {}),
              ...(filter.problem ? { problem: filter.problem } : {}),
              ...(filter.opportunity ? { opportunity: filter.opportunity } : {}),
            },
          },
          signal,
        }),
      ),
  });

/** One work item with its chain, the stages of its path and its ledger. */
export const loopItemQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: loopKeys.item(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/loop/items/{item_id}", { params: { path: { project, item_id: id } }, signal })),
  });

/** Each goal's metric measured now, with its work items by state. */
export const loopGoalsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: loopKeys.goals(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/loop/goals", { params: { path: { project } }, signal })),
  });

/** The project's goals as they are now, by rank. */
export const goalsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: loopKeys.goalList(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/goals", { params: { path: { project } }, signal })),
  });

/** One goal at its newest revision, by its code or alias. */
export const goalQuery = (api: ApiSource, project: string, ref: string) =>
  queryOptions({
    queryKey: loopKeys.goal(project, ref),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/goals/{ref}", { params: { path: { project, ref } }, signal })),
  });

/** Everything of the project that waits for a person, the three gates first. */
export const decisionBoardQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: loopKeys.board(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/decision-board", { params: { path: { project } }, signal })),
  });

/** The baseline of a goal that aims at an SRS document: what the hub proposes of each approved use case, with its evidence. */
export const baselineQuery = (api: ApiSource, project: string, ref: string) =>
  queryOptions({
    queryKey: loopKeys.baseline(project, ref),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/goals/{ref}/baseline", { params: { path: { project, ref } }, signal })),
  });

// Writes: an admin of the project alone; each fetches the session's CSRF header first, as every write of the web does.

/**
 * Turn the Loop on or off: the policy's next revision, the revision now with `loop.enabled` set, written only while
 * the policy is still at that revision; the hub answers 409 when another admin wrote it since it was read.
 */
export async function setLoopEnabled(api: ApiClient, project: string, enabled: boolean, note: string) {
  const now = await call(api.GET("/v1/projects/{project}/policy", { params: { path: { project } } }));
  const policy = { ...now.policy };
  const loop = typeof policy.loop === "object" && policy.loop !== null ? (policy.loop as Record<string, unknown>) : {};
  policy.loop = { ...loop, enabled };
  return writePolicy(api, project, { policy, note, if_revision: now.revision });
}

export async function resumeLoop(api: ApiClient, project: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/loop/resume", { params: { path: { project } }, headers }));
}

/** Confirm the baseline of a goal, once: the Loop then makes builds for its backlog. An admin of the project alone. */
export async function confirmBaseline(api: ApiClient, project: string, ref: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/goals/{ref}/baseline/confirm", { params: { path: { project, ref } }, headers }));
}
