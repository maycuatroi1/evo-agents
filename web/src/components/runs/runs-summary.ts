import { facetCount } from "./run-filters";
import { HELD_STATES, type Run, type RunList } from "./queries";

/** The summary cards above the runs list, from the project's active runs and the counts of every state. */

export type RunsSummary = {
  /** Runs a worker holds now, and how many workers hold them. */
  held: number;
  holdingWorkers: number;
  queued: number;
  /** ISO 8601; null when nothing waits, or the oldest waiting run is not among those read. */
  oldestQueuedAt: string | null;
  review: number;
  /** Plan runs parked for want of an answer: active, and waiting for a person. */
  parked: number;
  /** The one run waiting for review, when there is exactly one. */
  reviewRun: Run | null;
  done: number;
  failed: number;
  lost: number;
  cancelled: number;
  total: number;
};

/** The summary from the project's active runs and the counts of every state. */
export function summarizeRuns(list: RunList): RunsSummary {
  const { counts } = list;
  const held = HELD_STATES.reduce((sum, state) => sum + counts[state], 0);
  const holders = new Set<number>();
  let oldestQueuedAt: string | null = null;
  const inReview: Run[] = [];
  for (const run of list.runs) {
    if ((HELD_STATES as readonly string[]).includes(run.state) && run.worker_id !== null) holders.add(run.worker_id);
    if (run.state === "queued" && (!oldestQueuedAt || Date.parse(run.queued_at) < Date.parse(oldestQueuedAt))) {
      oldestQueuedAt = run.queued_at;
    }
    if (run.state === "review") inReview.push(run);
  }
  return {
    held,
    holdingWorkers: holders.size,
    queued: counts.queued,
    oldestQueuedAt,
    review: counts.review,
    parked: counts.parked,
    reviewRun: counts.review === 1 && inReview.length === 1 ? inReview[0] : null,
    done: counts.done,
    failed: counts.failed,
    lost: counts.lost,
    cancelled: counts.cancelled,
    total: facetCount(counts, null),
  };
}
