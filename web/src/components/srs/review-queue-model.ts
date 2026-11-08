import type { SrsQueuedUseCase, SrsQueuedVersion } from "./api";
import { reviewBroke, reviewHolds } from "./findings-model";
import { ownReview } from "./use-case-model";

/**
 * The pure model of the review queue (/p/{project}/x/srs/reviews): one card per use case waiting for review, said in
 * plain words. A card is what the viewer can do with it now (its status), who sent it, what changed and what @ba
 * found, and the cards waiting for the viewer come first. Nothing here reads the API.
 */

/** The queue reads again this often while @ba checks a version in it, so a card's status changes when it ends. */
export const QUEUE_REFRESH_MS = 15_000;

/** How long the lines of a card may be: what changed this time, and the use case's goal. */
export const SUMMARY_CHARS = 160;
export const INTENT_CHARS = 200;

/**
 * Where a card stands for the viewer, checked in this order: their own review (asked for changes, or approved), the
 * use case approved, a version they may not review, someone else's request for changes, @ba still checking, points
 * to settle before an approval (about this use case, else elsewhere in the document), else waiting for them.
 */
export type CardStatus =
  | { kind: "asked" }
  | { kind: "approved" }
  | { kind: "others" }
  | { kind: "changes" }
  | { kind: "ba_review" }
  | { kind: "deciding"; count: number }
  | { kind: "waiting" };

/** A card of the queue: a use case of a version in review, or a version that changes none (`useCase` null). */
export type QueueCard = { version: SrsQueuedVersion; useCase: SrsQueuedUseCase | null; status: CardStatus };

type Version = Pick<SrsQueuedVersion, "may_review" | "held_by" | "deciding" | "reviews" | "round">;

/** The review `login` gave a version that changes no use case, in its current round. */
function ownVersionReview(version: Pick<SrsQueuedVersion, "reviews" | "round">, login: string | null) {
  return login === null ? null : (version.reviews.find((review) => review.round === version.round && review.reviewer === login) ?? null);
}

/** What holds an approval the viewer could give: @ba's check, then the points to settle (this use case's first). */
function held(version: Version, deciding: number): CardStatus | null {
  if (version.held_by === "ba_review") return { kind: "ba_review" };
  if (deciding > 0) return { kind: "deciding", count: deciding };
  if (version.held_by === "findings" && version.deciding > 0) return { kind: "deciding", count: version.deciding };
  return null;
}

export function cardStatus(
  version: Version,
  useCase: Pick<SrsQueuedUseCase, "state" | "reviews" | "deciding"> | null,
  login: string | null,
): CardStatus {
  if (useCase === null) {
    const own = ownVersionReview(version, login);
    if (own) return { kind: own.decision === "approve" ? "approved" : "asked" };
    if (!version.may_review) return { kind: "others" };
    return held(version, 0) ?? { kind: "waiting" };
  }
  const own = ownReview(useCase, login);
  if (own?.decision === "request_changes") return { kind: "asked" };
  if (own?.decision === "approve" || useCase.state === "approved") return { kind: "approved" };
  if (!version.may_review) return { kind: "others" };
  if (useCase.state === "changes_requested") return { kind: "changes" };
  return held(version, useCase.deciding) ?? { kind: "waiting" };
}

/** Whether a card waits for the viewer: they can review it now, or settle what holds it first. */
export function waitsForViewer(status: CardStatus): boolean {
  return status.kind === "waiting" || status.kind === "deciding";
}

/** The cards of the queue in its order (the version submitted first, first; its use cases in the document's order). */
export function queueCards(queue: readonly SrsQueuedVersion[], login: string | null): { mine: QueueCard[]; others: QueueCard[] } {
  const mine: QueueCard[] = [];
  const others: QueueCard[] = [];
  for (const version of queue) {
    const cases = version.use_cases.length > 0 ? version.use_cases : [null];
    for (const useCase of cases) {
      const card = { version, useCase, status: cardStatus(version, useCase, login) };
      (waitsForViewer(card.status) ? mine : others).push(card);
    }
  }
  return { mine, others };
}

/** The card after `current` that waits for the viewer, else the first other one that does; null when none. */
export function nextCard(queue: readonly SrsQueuedVersion[], login: string | null, current: { document: string; number: number; key: string | null }): QueueCard | null {
  const { mine } = queueCards(queue, login);
  const isCurrent = (card: QueueCard) =>
    card.version.document === current.document && card.version.number === current.number && (card.useCase?.key ?? null) === current.key;
  const at = mine.findIndex(isCurrent);
  const after = at < 0 ? mine : [...mine.slice(at + 1), ...mine.slice(0, at)];
  return after.find((card) => !isCurrent(card)) ?? null;
}

/** What a card says about @ba's check of the version: still running, done with what it found, stopped, or nothing. */
export type BaLine =
  | { kind: "running" }
  | { kind: "here"; count: number }
  | { kind: "elsewhere"; count: number }
  | { kind: "clear" }
  | { kind: "broke" }
  | null;

export function baLine(version: Pick<SrsQueuedVersion, "review_run" | "held_by" | "deciding">, useCase: Pick<SrsQueuedUseCase, "deciding"> | null): BaLine {
  const run = version.review_run;
  if (version.held_by === "ba_review" || reviewHolds(run)) return { kind: "running" };
  if (reviewBroke(run)) return { kind: "broke" };
  const here = useCase?.deciding ?? 0;
  if (here > 0) return { kind: "here", count: here };
  if (version.deciding > 0) return { kind: "elsewhere", count: version.deciding };
  return run ? { kind: "clear" } : null;
}

/** Text of a field as one plain line: Markdown marks and links dropped, white space as one space. */
export function plainLine(text: string): string {
  return text
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/(\*\*|__|`)/g, "")
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** `text` as one plain line of at most `max` characters, cut at a word with an ellipsis when longer. */
export function clip(text: string, max: number): string {
  const line = plainLine(text);
  if (line.length <= max) return line;
  const cut = line.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,.;:]+$/, "")}…`;
}
