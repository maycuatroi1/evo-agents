import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the merge queue's pages and the Inbox read and write of a project's merge queue on GitLab and its autonomy
 * policy (plan gitlab-merge-queue; `evo_agents/hub/server/merge_view.py`, `autonomy_policies.py`). Any member with a
 * grant reads them; only an admin of the project answers an audit sample, approves a PROD release, asks for a revert,
 * resets a breaker or writes the policy. It lives in lib because the Inbox answers the queue's notices too.
 */
type Schemas = components["schemas"];
export type MergeQueueView = Schemas["MergeQueueView"];
export type QueuedMergeRequest = Schemas["QueuedMergeRequest"];
export type MergeEvent = Schemas["MergeEvent"];
export type MergeSample = Schemas["MergeSample"];
export type MergeBreaker = Schemas["MergeBreaker"];
export type BreakerReset = Schemas["BreakerReset"];
export type MergeFigures = Schemas["MergeFigures"];
export type MergeFigure = Schemas["MergeFigure"];
export type AutonomyPolicy = Schemas["AutonomyPolicy"];
export type PolicyWrite = Schemas["PolicyWrite"];
export type PolicyWritten = Schemas["PolicyWritten"];
export type PolicyRevision = Schemas["PolicyRevision"];
export type PolicyComparison = Schemas["PolicyComparison"];
export type PolicyChange = Schemas["PolicyChange"];
export type SampleResult = Schemas["SampleAnswer"]["result"];

/** `merge_gate.STATES`, in the order a merge request moves through them. */
export const MERGE_STATES = [
  "gating",
  "judging",
  "ready",
  "rebasing",
  "testing",
  "merging",
  "merged",
  "verifying",
  "releasing",
  "done",
  "rolling_back",
  "reverting",
  "reverted",
  "blocked",
  "paused",
  "closed",
] as const;
export type MergeState = (typeof MERGE_STATES)[number];

export function isMergeState(state: string): state is MergeState {
  return (MERGE_STATES as readonly string[]).includes(state);
}

/** `merge_gate.SOURCES`: a plan's run, a task run, the Curator's Builder, a person's label, the hub's revert. */
export const MERGE_SOURCES = ["plan", "task", "builder", "label", "revert"] as const;
export type MergeSource = (typeof MERGE_SOURCES)[number];

/** `merge_breaker.CAUSES`, in the order a trip names them. */
export const BREAKER_CAUSES = [
  "uat_check",
  "prod_check",
  "rollback",
  "revert",
  "target_red",
  "reverts_in_a_row",
  "revert_rate",
  "judge_misses",
] as const;
export type BreakerCause = (typeof BREAKER_CAUSES)[number];

/** `post_merge.RESULTS`: a check after the merge passed, failed, or gave no result. */
export const CHECK_RESULTS = ["passed", "failed", "none"] as const;
export type CheckResult = (typeof CHECK_RESULTS)[number];

/** `merge_gate.GATES`: 0 to 7, the cheap ones first. */
export const GATES = [0, 1, 2, 3, 4, 5, 6, 7] as const;

/** `merge_gate.EVENT_KINDS`: the lines of a merge request's ledger. */
export const EVENT_KINDS = [
  "opened",
  "queued",
  "head",
  "gate",
  "judged",
  "rebased",
  "pipeline",
  "merged",
  "uat_check",
  "prod_deploy",
  "prod_check",
  "retried",
  "rollback",
  "revert_opened",
  "reverted",
  "sampled",
  "sample_answered",
  "paused",
  "closed",
] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

/** `post_merge.AFTER_MERGE` of the server: the states in which an admin may ask the hub to revert a merge. */
export const REVERTABLE_STATES: readonly MergeState[] = ["merged", "verifying", "releasing", "done"];

/** The most merge requests one read of the queue lists (`merge_view.MAX_LIST`). */
export const MAX_QUEUE_LIST = 200;
/** An admin's note on an audit sample (`post_merge.MAX_NOTE_CHARS`). */
export const MAX_SAMPLE_NOTE_CHARS = 2000;
/** The window of the figures, in days, the pages offer. */
export const FIGURE_DAYS = [7, 30, 90] as const;
export type FigureDays = (typeof FIGURE_DAYS)[number];
/** The queue's pages ask the hub this often: merges move without anyone on the page. */
export const MERGE_QUEUE_REFRESH_MS = 15_000;

export type MergeQueueSegment = "" | "figures" | "policy";

export function mergeQueueHref(project: string, segment: MergeQueueSegment = ""): Route {
  const base = `/p/${encodeURIComponent(project)}/merge-queue`;
  return (segment ? `${base}/${segment}` : base) as Route;
}

/** A merge request of the queue opened over its list. */
export function mergeRequestHref(project: string, id: number): Route {
  return `${mergeQueueHref(project)}?mr=${id}` as Route;
}

export const mergeKeys = {
  /** Every read of a project's merge queue and policy: invalidated after an answer, a reset or a policy write. */
  all: (project: string) => ["projects", project, "merge-queue"] as const,
  queue: (project: string) => ["projects", project, "merge-queue", "list"] as const,
  figures: (project: string, days: number) => ["projects", project, "merge-queue", "figures", days] as const,
  policy: (project: string, revision: number | null) => ["projects", project, "merge-queue", "policy", revision] as const,
  revisions: (project: string) => ["projects", project, "merge-queue", "policy-revisions"] as const,
  compare: (project: string, from: number, to: number | null) => ["projects", project, "merge-queue", "policy-compare", from, to] as const,
};

/** The project's queue: its merge requests (the newest MAX_QUEUE_LIST), its audit samples and its breakers. */
export const mergeQueueQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: mergeKeys.queue(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/merge-queue", { params: { path: { project }, query: { limit: MAX_QUEUE_LIST } }, signal })),
  });

/** For each repo, agent and model, the merges of the last `days`. */
export const mergeFiguresQuery = (api: ApiSource, project: string, days: number) =>
  queryOptions({
    queryKey: mergeKeys.figures(project, days),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/merge-queue/figures", { params: { path: { project }, query: { days } }, signal })),
  });

/** The policy now, or revision `revision` whole. Revision 0 is a project that never had one. */
export const policyQuery = (api: ApiSource, project: string, revision: number | null = null) =>
  queryOptions({
    queryKey: mergeKeys.policy(project, revision),
    queryFn: ({ signal }) =>
      revision === null
        ? call(api().GET("/v1/projects/{project}/policy", { params: { path: { project } }, signal }))
        : call(api().GET("/v1/projects/{project}/policy/revisions/{revision}", { params: { path: { project, revision } }, signal })),
  });

/** Every revision of the policy, the newest first, each with the keys whose value it changed. */
export const policyRevisionsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: mergeKeys.revisions(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/policy/revisions", { params: { path: { project } }, signal })),
  });

/** The keys whose value differs between revisions `from` and `to` (the newest when null). */
export const policyCompareQuery = (api: ApiSource, project: string, from: number, to: number | null) =>
  queryOptions({
    queryKey: mergeKeys.compare(project, from, to),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/policy/compare", { params: { path: { project }, query: { from, ...(to === null ? {} : { to }) } }, signal })),
  });

// Writes: each fetches the session's CSRF header first, as every write of the web does.

export async function answerSample(api: ApiClient, project: string, sampleId: number, result: SampleResult, note: string | null) {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/merge-queue/samples/{sample_id}", {
      params: { path: { project, sample_id: sampleId } },
      body: { result, note: note || null },
      headers,
    }),
  );
}

export async function approveRelease(api: ApiClient, project: string, queueId: number) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/merge-queue/{queue_id}/release", { params: { path: { project, queue_id: queueId } }, headers }));
}

export async function askRevert(api: ApiClient, project: string, queueId: number) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/merge-queue/{queue_id}/revert", { params: { path: { project, queue_id: queueId } }, headers }));
}

export async function resumeRepo(api: ApiClient, project: string, repo: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/merge-queue/repos/{repo}/resume", { params: { path: { project, repo } }, headers }));
}

export async function writePolicy(api: ApiClient, project: string, body: PolicyWrite) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/policy", { params: { path: { project } }, body, headers }));
}
