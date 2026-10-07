import type { ParamSource } from "@/components/admin/data";
import { MESSAGE_STATES, parseRunId } from "@/components/runs/queries";
import { shortSha, utf8Bytes } from "@/components/runs/run-model";
import { PROJECT_NAME } from "@/lib/queries";

import {
  type Decision,
  INBOX_PAGE,
  type InboxQuery,
  MAX_ANSWER_BYTES,
  NOTIFICATION_KINDS,
  type Notification,
  type NotificationKind,
} from "./queries";

/**
 * The Inbox's pure parts: its filters in the URL, how a page of notifications splits into the decisions waiting for an
 * answer and everything else, the facts a notice carries, and what the answer form allows.
 */

/** The most pages the URL may name: 50 a page up to the API's largest offset. */
const MAX_PAGE = 2000;

export type InboxFilters = {
  kind: NotificationKind | null;
  project: string | null;
  unread: boolean;
  page: number;
  /** The decision shown in the sheet over the list, as `/inbox?decision=ID` names it. */
  decision: number | null;
};

export const NO_FILTERS: InboxFilters = { kind: null, project: null, unread: false, page: 1, decision: null };

function isKind(value: string | null): value is NotificationKind {
  return (NOTIFICATION_KINDS as readonly string[]).includes(value ?? "");
}

export function readInboxFilters(source: ParamSource): InboxFilters {
  const kind = source.get("kind");
  const project = source.get("project")?.trim() ?? "";
  const page = Number(source.get("page") ?? "1");
  return {
    kind: isKind(kind) ? kind : null,
    project: PROJECT_NAME.test(project) ? project : null,
    unread: source.get("unread") === "1",
    page: Number.isInteger(page) && page >= 1 && page <= MAX_PAGE ? page : 1,
    decision: parseRunId(source.get("decision")?.trim() ?? ""),
  };
}

/** The URL's query for a set of filters, in a stable order; empty when nothing is set. */
export function inboxSearch(filters: InboxFilters): string {
  const params = new URLSearchParams();
  if (filters.decision !== null) params.set("decision", String(filters.decision));
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.project) params.set("project", filters.project);
  if (filters.unread) params.set("unread", "1");
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** Whether the list is narrowed by a filter (the page and the decision shown are not filters). */
export function isFiltered(filters: InboxFilters): boolean {
  return filters.kind !== null || filters.project !== null || filters.unread;
}

/** What the API is asked for the list a set of filters shows. */
export function inboxQuery(filters: InboxFilters): InboxQuery {
  return {
    kind: filters.kind,
    project: filters.project,
    unread: filters.unread,
    limit: INBOX_PAGE,
    offset: (filters.page - 1) * INBOX_PAGE,
  };
}

/** A decision that still waits for its owner's answer. */
export function isOpenDecision(notification: Pick<Notification, "kind" | "decision_state">): boolean {
  return notification.kind === "decision" && notification.decision_state === "open";
}

/**
 * A page of notifications as the Inbox shows it: the decisions still open first (the API sends them first), then the
 * notices and the decisions answered, expired or cancelled, newest first.
 */
export function splitInbox(notifications: readonly Notification[]): { waiting: Notification[]; rest: Notification[] } {
  const waiting: Notification[] = [];
  const rest: Notification[] = [];
  for (const notification of notifications) (isOpenDecision(notification) ? waiting : rest).push(notification);
  return { waiting, rest };
}

/** The notifications of a page not read yet, at most as many as one request marks read. */
export function unreadIds(notifications: readonly Notification[], limit = 500): number[] {
  return notifications.filter((notification) => notification.read_at === null).map((notification) => notification.id).slice(0, limit);
}

/** The notification of decision `id` on a page, if the page holds it. */
export function notificationOfDecision(notifications: readonly Notification[], id: number): Notification | null {
  return notifications.find((notification) => notification.kind === "decision" && notification.decision_id === id) ?? null;
}

const COMMIT = /^[0-9a-f]{7,64}$/i;

export type NoticeFacts = { repo: string | null; branch: string | null; commits: string[] };

/** A notice's repo, branch and commits from its `details`, keeping only values of the right shape. */
export function noticeFacts(details: Notification["details"]): NoticeFacts {
  const text = (value: unknown) => (typeof value === "string" && value.trim() !== "" ? value : null);
  const commits = Array.isArray(details?.commits)
    ? details.commits.filter((commit): commit is string => typeof commit === "string" && COMMIT.test(commit))
    : [];
  return { repo: text(details?.repo), branch: text(details?.branch), commits };
}

/** A notice of a push or merge into a default branch: it names the repo, branch and commits. */
export function isBranchNotice(notification: Pick<Notification, "notice_kind">): boolean {
  return notification.notice_kind === "push_default_branch" || notification.notice_kind === "merge_default_branch";
}

/** Commits a notice lists before the rest fold under "and N more". */
export const COMMITS_SHOWN = 5;

/** A commit as people read it: its first 7 digits. */
export const shortCommit = shortSha;

/** The bell's number: up to 99, then 99+. */
export function bellNumber(unread: number): string {
  return unread > 99 ? "99+" : String(unread);
}

/** Why an answer cannot be sent as it stands: nothing chosen or written, or words over 4 KiB of UTF-8. */
export type AnswerProblem = "empty" | "long" | null;

export function answerProblem(option: string | null, text: string): AnswerProblem {
  if (option === null && text.trim() === "") return "empty";
  if (utf8Bytes(text.trim()) > MAX_ANSWER_BYTES) return "long";
  return null;
}

/** The body of an answer: the option chosen, and the words typed, when there are some. */
export function answerBody(option: string | null, text: string): { option?: string; text?: string } {
  const words = text.trim();
  return { ...(option !== null ? { option } : {}), ...(words ? { text: words } : {}) };
}

/**
 * Whether the visitor may answer a decision, and if not why: only its run's owner answers, only while it is open, and
 * only while its run is queued or held by a worker (an agent may still read the answer) or parked (the answer resumes
 * it). The API decides again on every answer.
 */
export type AnswerAccess = "answer" | "notOwner" | "closed" | "runGone";

export function answerAccess(decision: Pick<Decision, "state" | "owner" | "run_state">, login: string | null): AnswerAccess {
  if (decision.state !== "open") return "closed";
  if (login === null || login !== decision.owner) return "notOwner";
  if (decision.run_state !== "parked" && !(MESSAGE_STATES as readonly string[]).includes(decision.run_state)) return "runGone";
  return "answer";
}

/** The option an answer chose, as the decision lists it. */
export function chosenOption(decision: Pick<Decision, "options" | "answer_option">) {
  if (decision.answer_option === null) return null;
  return decision.options.find((option) => option.key === decision.answer_option) ?? null;
}

/**
 * The option picked for the owner when the form opens: the one the agent recommends, as the kit's DecisionCard has it,
 * so one click answers with the agent's pick. Null when the agent recommends none, or names a key it did not offer.
 */
export function initialOption(decision: Pick<Decision, "options" | "recommended">): string | null {
  const key = decision.recommended ?? decision.options.find((option) => option.recommended)?.key ?? null;
  return key !== null && decision.options.some((option) => option.key === key) ? key : null;
}

/**
 * When a run waiting for this decision parks, as the decision's head says it: in so long while it waits, since when
 * once it parked, nothing while its agent still works or before the hub has said (`parksAt` from GET /v1/me/overview).
 */
export type ParkTiming = { kind: "parksIn"; ms: number } | { kind: "parked"; at: string } | null;

export function parkTiming(decision: Pick<Decision, "state" | "run_state">, parksAt: string | null, now: number | null): ParkTiming {
  if (decision.state !== "open" || parksAt === null) return null;
  if (decision.run_state === "parked") return { kind: "parked", at: parksAt };
  if (decision.run_state !== "waiting" || now === null) return null;
  return { kind: "parksIn", ms: Math.max(0, Date.parse(parksAt) - now) };
}
