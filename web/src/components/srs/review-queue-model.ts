import type { SrsQueuedVersion, SrsUseCase } from "./api";
import { ownReview } from "./use-case-model";

/**
 * The pure model of the review queue (/p/{project}/x/srs/reviews): the versions in review of the documents the viewer
 * sees, each with its use cases to review, and which of them wait for the viewer.
 */

/** What the queue shows: what waits for the viewer, or every version in review. */
export type QueueFilter = "mine" | "all";

/** The queue reads again this often while @ba reviews a version in it, so the version's line changes when it ends. */
export const QUEUE_REFRESH_MS = 15_000;

type Reviewable = Pick<SrsQueuedVersion, "may_review">;

/** Whether a use case of `version` waits for `login`: they may review the version now, it is pending and no review of theirs counts on it. */
export function caseWaitsFor(version: Reviewable, useCase: Pick<SrsUseCase, "state" | "reviews">, login: string | null): boolean {
  return version.may_review && useCase.state === "pending" && ownReview(useCase, login) === null;
}

/**
 * Whether a version that changes no use case waits for `login`: a review of the whole version approves it, so it waits
 * while they may review it and have not reviewed its current round.
 */
export function versionWaitsFor(version: Pick<SrsQueuedVersion, "may_review" | "use_cases" | "reviews" | "round">, login: string | null): boolean {
  if (!version.may_review || version.use_cases.length > 0) return false;
  return !version.reviews.some((review) => review.round === version.round && review.reviewer === login);
}

/** Whether the viewer may act on a use case from the queue: they may review its version and it still waits for approvals. */
export function mayReviewCase(version: Reviewable, useCase: Pick<SrsUseCase, "state">): boolean {
  return version.may_review && useCase.state === "pending";
}

/** The filter a visitor lands on: what waits for them when they may review any version, else every version. */
export function defaultFilter(queue: readonly Reviewable[]): QueueFilter {
  return queue.some((version) => version.may_review) ? "mine" : "all";
}

/** A version of the queue with the use cases the filter shows under it. */
export type QueueGroup = { version: SrsQueuedVersion; cases: SrsUseCase[] };

/**
 * The versions and use cases `filter` shows, in the queue's order (submitted first, first). Under "mine", only the use
 * cases waiting for `login`, and the versions that hold one or wait for a review of their own.
 */
export function queueGroups(queue: readonly SrsQueuedVersion[], filter: QueueFilter, login: string | null): QueueGroup[] {
  if (filter === "all") return queue.map((version) => ({ version, cases: version.use_cases }));
  return queue.flatMap((version) => {
    const cases = version.use_cases.filter((useCase) => caseWaitsFor(version, useCase, login));
    return cases.length > 0 || versionWaitsFor(version, login) ? [{ version, cases }] : [];
  });
}

/** What the groups hold to review: each use case, and a version that changes none as one. */
export function toReview(groups: readonly QueueGroup[]): number {
  return groups.reduce((sum, group) => sum + Math.max(group.cases.length, 1), 0);
}

/** The queue's counts: versions in review, what they hold to review, and how much of it waits for `login`. */
export function queueCounts(queue: readonly SrsQueuedVersion[], login: string | null): { versions: number; all: number; mine: number } {
  return { versions: queue.length, all: toReview(queueGroups(queue, "all", login)), mine: toReview(queueGroups(queue, "mine", login)) };
}
