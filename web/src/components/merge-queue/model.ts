import {
  type CheckResult,
  CHECK_RESULTS,
  FIGURE_DAYS,
  type FigureDays,
  isMergeState,
  type MergeBreaker,
  type MergeSample,
  type MergeState,
  type QueuedMergeRequest,
  REVERTABLE_STATES,
} from "@/lib/merge-queue";

/**
 * The merge queue's page without React: where each merge request stands as the page groups it, the counts of its
 * strip, the filters the URL holds and what an admin may do to a row.
 */

/** The groups the page filters by, in the order a merge request moves through them. */
export const STATE_GROUPS = ["waiting", "turn", "blocked", "after", "finished"] as const;
export type StateGroup = (typeof STATE_GROUPS)[number];

const GROUP_OF: Record<MergeState, StateGroup> = {
  gating: "waiting",
  judging: "waiting",
  ready: "waiting",
  paused: "waiting",
  rebasing: "turn",
  testing: "turn",
  merging: "turn",
  blocked: "blocked",
  merged: "after",
  verifying: "after",
  releasing: "after",
  rolling_back: "after",
  reverting: "after",
  done: "finished",
  reverted: "finished",
  closed: "finished",
};

/** The group of a state; a state this web does not know reads as waiting, so it is never hidden as finished. */
export function groupOf(state: string): StateGroup {
  return isMergeState(state) ? GROUP_OF[state] : "waiting";
}

export type QueueFilters = { group: StateGroup | null; repo: string | null; open: number | null };

function one(value: string | string[] | null | undefined): string | null {
  return (Array.isArray(value) ? value[0] : value) ?? null;
}

function positive(value: string | null): number | null {
  if (value === null || !/^\d{1,18}$/.test(value)) return null;
  const number = Number(value);
  return number > 0 ? number : null;
}

/** The page's state in its URL: `?state=blocked`, `?repo=shop`, `?mr=12` (the merge request open in the sheet). */
export function readQueueFilters(params: { get(name: string): string | null } | Record<string, string | string[] | undefined>): QueueFilters {
  const get = (name: string) => ("get" in params && typeof params.get === "function" ? params.get(name) : one((params as Record<string, string | string[] | undefined>)[name]));
  const group = get("state");
  return {
    group: (STATE_GROUPS as readonly string[]).includes(group ?? "") ? (group as StateGroup) : null,
    repo: get("repo") || null,
    open: positive(get("mr")),
  };
}

/** The query string of `filters`, without the leading `?`; empty for none. */
export function queueSearch(filters: QueueFilters): string {
  const params = new URLSearchParams();
  if (filters.group) params.set("state", filters.group);
  if (filters.repo) params.set("repo", filters.repo);
  if (filters.open !== null) params.set("mr", String(filters.open));
  return params.toString();
}

export function countByGroup(requests: readonly Pick<QueuedMergeRequest, "state">[]): Record<StateGroup, number> {
  const counts = Object.fromEntries(STATE_GROUPS.map((group) => [group, 0])) as Record<StateGroup, number>;
  for (const request of requests) counts[groupOf(request.state)] += 1;
  return counts;
}

export function filterRequests(requests: readonly QueuedMergeRequest[], filters: Pick<QueueFilters, "group" | "repo">): QueuedMergeRequest[] {
  return requests.filter(
    (request) => (filters.group === null || groupOf(request.state) === filters.group) && (filters.repo === null || request.repo === filters.repo),
  );
}

/** The repos the queue names, its breakers' and its merge requests', sorted. */
export function queueRepos(requests: readonly Pick<QueuedMergeRequest, "repo">[], breakers: readonly Pick<MergeBreaker, "repo">[]): string[] {
  return [...new Set([...breakers.map((breaker) => breaker.repo), ...requests.map((request) => request.repo)])].sort((a, b) => a.localeCompare(b));
}

export function trippedBreakers(breakers: readonly MergeBreaker[]): MergeBreaker[] {
  return breakers.filter((breaker) => breaker.state === "tripped");
}

/** The audit samples an admin has not answered yet. */
export function waitingSamples(samples: readonly MergeSample[]): MergeSample[] {
  return samples.filter((sample) => sample.result === null);
}

/** The merge request whose turn it is on each target branch, the oldest waiting one, and so on: the strip's lines. */
export function oldestReady(requests: readonly QueuedMergeRequest[]): QueuedMergeRequest | null {
  const ready = requests.filter((request) => request.state === "ready" && request.queued_at);
  ready.sort((a, b) => Date.parse(a.queued_at ?? "") - Date.parse(b.queued_at ?? ""));
  return ready[0] ?? null;
}

export function isCheckResult(result: string | null | undefined): result is CheckResult {
  return result !== null && result !== undefined && (CHECK_RESULTS as readonly string[]).includes(result);
}

/**
 * Whether an admin may approve the PROD release of `request` now: the policy releases its repo by hand, its UAT check
 * passed and it waits in `releasing` (`post_merge.approve_release` of the server refuses anything else).
 */
export function canApproveRelease(request: Pick<QueuedMergeRequest, "state" | "uat_result">, release: string | null): boolean {
  return release === "manual" && request.state === "releasing" && request.uat_result === "passed";
}

/** Whether an admin may ask the hub to revert `request`: a merge it followed after its merge, never a revert. */
export function canAskRevert(request: Pick<QueuedMergeRequest, "state" | "source">): boolean {
  return request.source !== "revert" && isMergeState(request.state) && REVERTABLE_STATES.includes(request.state);
}

/** `!12`, or the queue's own id while the hub has not opened the merge request. */
export function requestName(request: Pick<QueuedMergeRequest, "iid" | "id">): string {
  return request.iid !== null ? `!${request.iid}` : `#${request.id}`;
}

/** A commit as people read it: its first 8 characters. */
export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 8) : "";
}

/** Only an http(s) link of the forge opens from the page; anything else the hub stored is shown as text. */
export function forgeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

/** A dimension a Judge judged on its own (`{"verdict": "pass" | "fail", "reason": ...}`). */
export type DimensionLine = { name: string; passed: boolean | null; reason: string | null };

/** One Judge's verdict as the hub kept it (`merge_judge.entry`): the model, the verdict, why not, each dimension. */
export type JudgeVerdict = {
  judge: "first" | "second";
  model: string | null;
  passed: boolean | null;
  runId: number | null;
  failures: string[];
  dimensions: DimensionLine[];
};

const record = (value: unknown): Record<string, unknown> => (value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {});
const text = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

/** The Judges' verdicts of a merge request, the first then the second (a high risk alone has one). */
export function judgeVerdicts(verdict: Record<string, unknown> | null | undefined): JudgeVerdict[] {
  const found: JudgeVerdict[] = [];
  for (const judge of ["first", "second"] as const) {
    if (!verdict || !(judge in verdict)) continue;
    const entry = record(verdict[judge]);
    const dimensions = Object.entries(record(entry.dimensions)).map(([name, value]): DimensionLine => {
      const line = record(value);
      const word = text(line.verdict);
      return { name, passed: word === null ? null : word === "pass", reason: text(line.reason) };
    });
    found.push({
      judge,
      model: text(entry.model),
      passed: typeof entry.passed === "boolean" ? entry.passed : null,
      runId: typeof entry.run_id === "number" ? entry.run_id : null,
      failures: Array.isArray(entry.failures) ? entry.failures.filter((item): item is string => typeof item === "string") : [],
      dimensions,
    });
  }
  return found;
}

/** The risk of a merge request as the gate wrote it: high or not, and the reasons. */
export function riskOf(risk: Record<string, unknown> | null | undefined): { high: boolean; reasons: string[] } | null {
  if (!risk) return null;
  const reasons = Array.isArray(risk.reasons) ? risk.reasons.filter((reason): reason is string => typeof reason === "string") : [];
  return { high: risk.high === true, reasons };
}

/** `?days=7`, 30 without it, or 90: the window of the figures. */
export function readDays(value: string | string[] | null | undefined): FigureDays {
  const raw = Number(Array.isArray(value) ? value[0] : value);
  return (FIGURE_DAYS as readonly number[]).includes(raw) ? (raw as FigureDays) : 30;
}
