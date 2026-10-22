import type { SrsUseCase, SrsUseCaseReview } from "./api";
import type { ReviewBlock } from "./model";

/**
 * The pure model of the use cases a version in review needs reviewed (evo_agents.core_plugins.srs): how many stand
 * where, what the viewer already said on one, where a use case is read and which role their reviews count for.
 */

export type CaseSummary = { total: number; approved: number; pending: number; asked: number };

export function summarize(cases: readonly SrsUseCase[]): CaseSummary {
  const count = (state: SrsUseCase["state"]) => cases.filter((one) => one.state === state).length;
  return { total: cases.length, approved: count("approved"), pending: count("pending"), asked: count("changes_requested") };
}

/** Whether a use case holds the version: it is asked for changes, so no approval of the version goes through. */
export function holdsVersion(cases: readonly SrsUseCase[]): boolean {
  return cases.some((one) => one.state === "changes_requested");
}

/**
 * Why `viewer` cannot review a use case: the reasons they cannot review the version, but "reviewed". A later review
 * of theirs on a use case replaces the earlier one, so having reviewed the version or the use case blocks nothing.
 */
export function caseBlock(block: ReviewBlock | null): Exclude<ReviewBlock, "reviewed"> | null {
  return block === "reviewed" ? null : block;
}

/** What a use case is called to a person: its code, else its title. */
export function caseName(useCase: Pick<SrsUseCase, "code" | "title">): string {
  return useCase.code ?? useCase.title;
}

/** The ref that names the use case to the hub and to its page: its code, else its key. */
export function caseRef(useCase: Pick<SrsUseCase, "code" | "key">): string {
  return useCase.code ?? useCase.key;
}

/** The review of `login` that counts on a use case now, if any. */
export function ownReview(useCase: Pick<SrsUseCase, "reviews">, login: string | null): SrsUseCaseReview | null {
  return login === null ? null : (useCase.reviews.find((review) => review.reviewer === login) ?? null);
}

/** A review that counts here but was recorded on an earlier version, while the use case read the same. */
export function carriedFrom(review: Pick<SrsUseCaseReview, "version">, version: number): number | null {
  return review.version < version ? review.version : null;
}

/**
 * The version a use case's page is read at: the version under review, or, for a use case it drops, the one approved
 * last, which still holds it. Null when there is none, and the page opens at its default.
 */
export function caseVersion(useCase: Pick<SrsUseCase, "change">, version: number, approved: number | null): number | null {
  return useCase.change === "removed" ? approved : version;
}

/** The use case an item is, among those the version needs reviewed; null for another kind of item or a use case that needs none. */
export function caseOf(cases: readonly SrsUseCase[], item: { key: string; kind: string }): SrsUseCase | null {
  return item.kind === "use_case" ? (cases.find((one) => one.key === item.key) ?? null) : null;
}
