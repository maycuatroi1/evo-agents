import type { SrsFinding, SrsReviewRun } from "./api";

/**
 * The pure model of a document's findings on a version's review page (criterion a14 of plan srs-conflicts-and-tasks):
 * the order and counts the Findings card lists them in, who settled one, whether @ba's review of the round still holds
 * approval, and why Approve is locked, as the hub's gate decides it (evo_agents.hub.server.srs.findings,
 * approval_hold). The hub stays the gate: an approval this model lets through may still meet a 409, after which the
 * page reads the findings again. Nothing here reads the API.
 */

type RunState = SrsReviewRun["state"];
export type FindingLevel = SrsFinding["level"];
export type FindingKind = SrsFinding["kind"];
export type FindingStatus = SrsFinding["status"];

/** How often the page reads the document and its findings again while @ba's review holds approval. */
export const REVIEW_REFRESH_MS = 5_000;

/**
 * The states in which @ba's review of a round holds approval (`runs.ACTIVE_STATES`): queued, held by a worker, or
 * waiting. A run that is done, failed, lost or cancelled holds nothing.
 */
const HOLDING: ReadonlySet<RunState> = new Set<RunState>(["queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked"]);
/** The ends of a review run worth saying: approval no longer waits for it, and the list may miss what it did not record. */
const BROKEN: ReadonlySet<RunState> = new Set<RunState>(["failed", "lost", "cancelled"]);

export function reviewHolds(run: SrsReviewRun | null | undefined): run is SrsReviewRun {
  return run != null && HOLDING.has(run.state);
}

export function reviewBroke(run: SrsReviewRun | null | undefined): run is SrsReviewRun {
  return run != null && BROKEN.has(run.state);
}

/** An open finding of level decide: what holds the approval of every version of its document. */
export function isDeciding(finding: Pick<SrsFinding, "status" | "level">): boolean {
  return finding.status === "open" && finding.level === "decide";
}

const LEVEL_ORDER: Record<FindingLevel, number> = { decide: 0, advisory: 1, info: 2 };

export type FindingGroups = { decide: SrsFinding[]; open: SrsFinding[]; settled: SrsFinding[] };

/**
 * The findings as the card lists them: the open ones to decide first, oldest first; then the other open ones, advisory
 * before info; then the settled ones, the one settled last first.
 */
export function groupFindings(findings: readonly SrsFinding[]): FindingGroups {
  const open = findings.filter((finding) => finding.status === "open");
  const settled = findings.filter((finding) => finding.status !== "open");
  const byId = (a: SrsFinding, b: SrsFinding) => a.id - b.id;
  const settledAt = (finding: SrsFinding) => (finding.settled_at ? Date.parse(finding.settled_at) : 0);
  return {
    decide: open.filter(isDeciding).sort(byId),
    open: open.filter((finding) => !isDeciding(finding)).sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level] || byId(a, b)),
    settled: [...settled].sort((a, b) => settledAt(b) - settledAt(a) || b.id - a.id),
  };
}

/** Who settled a finding: a member (in its chat, or an admin's waiver), the knowledge graph, or @ba's own waiver. */
export type Settler = { by: "member"; login: string } | { by: "graph" } | { by: "agent" };

export function settlerOf(finding: Pick<SrsFinding, "status" | "settled_by">): Settler | null {
  if (finding.status === "open") return null;
  if (finding.settled_by) return { by: "member", login: finding.settled_by };
  return finding.status === "resolved" ? { by: "graph" } : { by: "agent" };
}

/** Why Approve is locked: the open findings to decide, with the chat they are settled in; or @ba's review still running. */
export type ApproveLock = { kind: "findings"; count: number; chat: string | null } | { kind: "review"; run: SrsReviewRun };

/**
 * Why Approve is locked now, checked in the hub's order: open findings to decide first, then @ba's review of the
 * version's round while it is queued or running; null when neither holds it. Findings not read yet (`undefined`) hold
 * nothing here: the hub's 409 still refuses the approval.
 */
export function approveLock(findings: readonly SrsFinding[] | undefined, run: SrsReviewRun | null): ApproveLock | null {
  const deciding = (findings ?? []).filter(isDeciding).sort((a, b) => a.id - b.id);
  if (deciding.length > 0) {
    const chat = deciding.map((finding) => finding.chat_link).find((link) => chatTarget(link) !== null) ?? null;
    return { kind: "findings", count: deciding.length, chat };
  }
  return reviewHolds(run) ? { kind: "review", run } : null;
}

export type ChatTarget = { href: string; external: boolean };

/**
 * Where a finding's chat link goes: a path of the hub's web (`/chats/12?message=40`), or an http(s) URL out of it; null
 * for anything else, as the hub takes only those. A path that starts with two slashes names another host, so it is
 * not taken as a path.
 */
export function chatTarget(link: string | null | undefined): ChatTarget | null {
  if (!link) return null;
  if (/^\/(?![/\\])\S*$/.test(link)) return { href: link, external: false };
  if (/^https?:\/\/\S+$/i.test(link)) return { href: link, external: true };
  return null;
}
