import type { ParamSource } from "@/components/admin/data";
import { MESSAGE_STATES, parseRunId } from "@/components/runs/queries";
import { shortSha, utf8Bytes } from "@/components/runs/run-model";
import { PROJECT_NAME } from "@/lib/queries";

import {
  type Decision,
  HUB_DECISION_CATEGORIES,
  INBOX_PAGE,
  type InboxQuery,
  MAX_ANSWER_BYTES,
  NOTIFICATION_KINDS,
  type Notification,
  type NotificationKind,
} from "./queries";

/**
 * The Inbox's pure parts: its filters in the URL, how a page of notifications splits into the decisions waiting for an
 * answer and everything else, how everything else groups by day and by run, the facts a notice carries, and what the
 * answer form allows.
 */

/** The most pages the URL may name: 50 a page up to the API's largest offset. */
const MAX_PAGE = 2000;
/** `MAX_QUERY_CHARS` of the API: the words the list searches the titles and bodies for. */
export const MAX_QUERY_CHARS = 200;

export type InboxFilters = {
  kind: NotificationKind | null;
  project: string | null;
  unread: boolean;
  /** Words of the title or the body, as `?q=` names them; empty for none. */
  q: string;
  /** Only the notifications of this run, as `?run=ID` names it. */
  run: number | null;
  page: number;
  /** The decision shown in the sheet over the list, as `/inbox?decision=ID` names it. */
  decision: number | null;
  /** The proposal of the Curator shown in the sheet over the list, as `/inbox?proposal=ID` names it. */
  proposal: number | null;
};

export const NO_FILTERS: InboxFilters = {
  kind: null,
  project: null,
  unread: false,
  q: "",
  run: null,
  page: 1,
  decision: null,
  proposal: null,
};

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
    q: [...(source.get("q")?.trim() ?? "")].slice(0, MAX_QUERY_CHARS).join("").trim(),
    run: parseRunId(source.get("run")?.trim() ?? ""),
    page: Number.isInteger(page) && page >= 1 && page <= MAX_PAGE ? page : 1,
    decision: parseRunId(source.get("decision")?.trim() ?? ""),
    proposal: parseRunId(source.get("proposal")?.trim() ?? ""),
  };
}

/** The URL's query for a set of filters, in a stable order; empty when nothing is set. */
export function inboxSearch(filters: InboxFilters): string {
  const params = new URLSearchParams();
  if (filters.decision !== null) params.set("decision", String(filters.decision));
  if (filters.proposal !== null) params.set("proposal", String(filters.proposal));
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.project) params.set("project", filters.project);
  if (filters.unread) params.set("unread", "1");
  if (filters.run !== null) params.set("run", String(filters.run));
  if (filters.q) params.set("q", filters.q);
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** Whether the list is narrowed by a filter (the page and the decision or proposal shown are not filters). */
export function isFiltered(filters: InboxFilters): boolean {
  return filters.kind !== null || filters.project !== null || filters.unread || filters.q !== "" || filters.run !== null;
}

/** What the API is asked for the list a set of filters shows. */
export function inboxQuery(filters: InboxFilters): InboxQuery {
  return {
    kind: filters.kind,
    project: filters.project,
    unread: filters.unread,
    q: filters.q,
    run: filters.run,
    limit: INBOX_PAGE,
    offset: (filters.page - 1) * INBOX_PAGE,
  };
}

/** A decision that still waits for its owner's answer. */
export function isOpenDecision(notification: Pick<Notification, "kind" | "decision_state">): boolean {
  return notification.kind === "decision" && notification.decision_state === "open";
}

/** A tier 2 proposal of the Curator that still waits for an admin's answer. */
export function isOpenProposal(notification: Pick<Notification, "kind" | "proposal_state">): boolean {
  return notification.kind === "proposal" && notification.proposal_state === "open";
}

/** What waits for an answer: an open decision, or an open proposal. */
export function isWaiting(notification: Pick<Notification, "kind" | "decision_state" | "proposal_state">): boolean {
  return isOpenDecision(notification) || isOpenProposal(notification);
}

/**
 * A page of notifications as the Inbox shows it: the decisions and the proposals still open first (the API sends them
 * first), then the notices and the decisions and proposals answered, expired or cancelled, newest first.
 */
export function splitInbox(notifications: readonly Notification[]): { waiting: Notification[]; rest: Notification[] } {
  const waiting: Notification[] = [];
  const rest: Notification[] = [];
  for (const notification of notifications) (isWaiting(notification) ? waiting : rest).push(notification);
  return { waiting, rest };
}

/** A day of the calendar of `timeZone` (the one the page formats dates in) as `YYYY-MM-DD`. */
export function dayKey(iso: string, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(iso));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** How many days `key` lies before `today`, both `YYYY-MM-DD`: 0 today, 1 yesterday. */
export function daysBefore(key: string, today: string): number {
  return Math.round((Date.parse(`${today}T00:00:00Z`) - Date.parse(`${key}T00:00:00Z`)) / 86_400_000);
}

/**
 * One line of the list below what waits for an answer: a notification alone, or the notifications one run sent on
 * one day, newest first, which show as one line that opens to each of them.
 */
export type InboxEntry =
  | { type: "single"; notification: Notification }
  | { type: "run"; key: string; project: string | null; runId: number; notifications: Notification[]; unread: number };

export type InboxDay = { key: string; entries: InboxEntry[]; total: number; unread: number };

/**
 * The notices and the decisions and proposals no longer open, as the Inbox lists them: by day of `timeZone`, newest
 * first, and within a day the notifications of one run together, where the newest of them stands. The order of the
 * page (newest first) is kept.
 */
export function groupInbox(notifications: readonly Notification[], timeZone: string): InboxDay[] {
  const days = new Map<string, { key: string; entries: (InboxEntry & { type: "run" })[]; total: number; unread: number }>();
  const runs = new Map<string, InboxEntry & { type: "run" }>();
  for (const notification of notifications) {
    const key = dayKey(notification.created_at, timeZone);
    let day = days.get(key);
    if (!day) {
      day = { key, entries: [], total: 0, unread: 0 };
      days.set(key, day);
    }
    const unread = notification.read_at === null ? 1 : 0;
    day.total += 1;
    day.unread += unread;
    // A notification of no run stands alone: a key no other one shares.
    const runKey = notification.run_id !== null ? `${key}/${notification.project ?? ""}#${notification.run_id}` : `${key}/id${notification.id}`;
    const found = runs.get(runKey);
    if (found) {
      found.notifications.push(notification);
      found.unread += unread;
      continue;
    }
    const entry = { type: "run" as const, key: runKey, project: notification.project, runId: notification.run_id ?? 0, notifications: [notification], unread };
    runs.set(runKey, entry);
    day.entries.push(entry);
  }
  return [...days.values()].map((day) => ({
    ...day,
    entries: day.entries.map((entry): InboxEntry =>
      entry.notifications.length === 1 ? { type: "single", notification: entry.notifications[0] } : entry,
    ),
  }));
}

/**
 * Whether a notice opens in the list by itself while unread: it asks something of its reader (an answer of the merge
 * queue, a baseline to review, a page that wants a sign-in). Everything else, the Curator's morning brief of each
 * project included, shows its title and the first line of its body until opened.
 */
export function opensByItself(notification: Pick<Notification, "kind" | "notice_kind" | "details" | "project" | "read_at">): boolean {
  if (notification.kind !== "notice" || notification.read_at !== null) return false;
  if (mergeAsk(notification) !== null) return true;
  return notification.notice_kind === "canvas_baseline" || notification.notice_kind === "browser_needs_login";
}

/** The first line of a body with words in it, without Markdown's marks at its start, for the line under a title. */
export function snippet(body: string | null): string | null {
  if (!body) return null;
  const line = body
    .split("\n")
    .map((text) => text.replace(/^\s*(?:[#>*-]+|\d+\.)\s*/, "").replace(/[*_`]/g, "").trim())
    .find((text) => text !== "");
  return line ?? null;
}

/** The notifications of a page not read yet, at most as many as one request marks read. */
export function unreadIds(notifications: readonly Notification[], limit = 500): number[] {
  return notifications.filter((notification) => notification.read_at === null).map((notification) => notification.id).slice(0, limit);
}

/** The notification of proposal `id` on a page, if the page holds it. */
export function notificationOfProposal(notifications: readonly Notification[], id: number): Notification | null {
  return notifications.find((notification) => notification.kind === "proposal" && notification.proposal_id === id) ?? null;
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

/**
 * A notice of a push or merge into a default branch, or of a repo's merge queue that stopped: it names the repo, the
 * branch and the commits.
 */
export function isBranchNotice(notification: Pick<Notification, "notice_kind">): boolean {
  return (
    notification.notice_kind === "push_default_branch" ||
    notification.notice_kind === "merge_default_branch" ||
    notification.notice_kind === "merge_paused"
  );
}

export type PluginFacts = { plugin: string | null; topic: string | null };

/** The plugin that sent a notice and its topic, from `details`, keeping only text. */
export function pluginFacts(details: Notification["details"]): PluginFacts {
  const text = (value: unknown) => (typeof value === "string" && value.trim() !== "" ? value : null);
  return { plugin: text(details?.plugin), topic: text(details?.topic) };
}

/** Commits a notice lists before the rest fold under "and N more". */
export const COMMITS_SHOWN = 5;

/** A commit as people read it: its first 7 digits. */
export const shortCommit = shortSha;

/** The bell's number: up to 99, then 99+. */
export function bellNumber(unread: number): string {
  return unread > 99 ? "99+" : String(unread);
}

/**
 * Why an answer cannot be sent as it stands: nothing chosen or written, no option for a decision of the hub's, words
 * with an option that keeps the decision open (the agent would never read them), or words over 4 KiB of UTF-8.
 */
export type AnswerProblem = "empty" | "option" | "waits" | "long" | null;

/** Whether option `key` of `decision` says not yet: choosing it keeps the decision open and tells the agent nothing. */
export function optionWaits(decision: Pick<Decision, "options">, key: string | null): boolean {
  return key !== null && decision.options.some((option) => option.key === key && option.waits === true);
}

/** Whether the hub itself does what the answer of `decision` chooses (`runs.HUB_DECISION_CATEGORIES`). */
export function isHubDecision(decision: Pick<Decision, "category">): boolean {
  return (HUB_DECISION_CATEGORIES as readonly string[]).includes(decision.category);
}

/** The words of the link to the run a decision names: a triage run, a release run, an author run (no plan), a plan's run. */
export function runLinkWords(decision: Pick<Decision, "category" | "plan_id">) {
  if (decision.category === "triage") return "triageRunLink" as const;
  if (decision.category === "release") return "releaseRunLink" as const;
  return decision.plan_id === null ? ("authorRunLink" as const) : ("runLink" as const);
}

/**
 * What keeps an answer from going: nothing chosen or written, no option for a decision of the hub's, words with an
 * option that keeps the decision open (`waits`), words too long.
 */
export function answerProblem(option: string | null, text: string, hub = false, waits = false): AnswerProblem {
  if (option === null && text.trim() === "") return "empty";
  if (option === null && hub) return "option";
  if (waits && text.trim() !== "") return "waits";
  if (utf8Bytes(text.trim()) > MAX_ANSWER_BYTES) return "long";
  return null;
}

/** The body of an answer: the option chosen, and the words typed, when there are some. */
export function answerBody(option: string | null, text: string): { option?: string; text?: string } {
  const words = text.trim();
  return { ...(option !== null ? { option } : {}), ...(words ? { text: words } : {}) };
}

/**
 * Whether the visitor may answer a decision, and if not why: only the person it is asked of answers, only while it is open, and
 * only while its run is queued or held by a worker (an agent may still read the answer), parked (the answer resumes
 * it) or, for a local session, idle (it takes the answer at its next turn). A decision of the hub's own takes its
 * answer whatever its run is: the hub, not an agent, does what it chooses. The API decides again on every answer.
 */
export type AnswerAccess = "answer" | "notOwner" | "closed" | "runGone";

/** Who a decision is asked of, who answers it: `asked_of`, or its run's owner from a hub that does not send it. */
export function askedOf(decision: Pick<Decision, "owner"> & Partial<Pick<Decision, "asked_of">>): string {
  return decision.asked_of ?? decision.owner;
}

export function answerAccess(
  decision: Pick<Decision, "state" | "owner" | "run_state" | "category"> & Partial<Pick<Decision, "asked_of">>,
  login: string | null,
): AnswerAccess {
  if (decision.state !== "open") return "closed";
  if (login === null || login !== askedOf(decision)) return "notOwner";
  if (isHubDecision(decision)) return "answer";
  // idle: a local session whose turn ended; it takes the answer at its next turn.
  if (decision.run_state !== "parked" && decision.run_state !== "idle" && !(MESSAGE_STATES as readonly string[]).includes(decision.run_state)) return "runGone";
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

/** What the hub asks an admin in a notice of its merge queue (`components/inbox/merge-notice.tsx` answers it). */
export type MergeAsk = "sample" | "release" | "revert" | "resume";

/**
 * The question of a notice of the merge queue: an audit sample to answer (`details.ask` sample), a PROD release to
 * approve (release), a suspect to revert (revert), or, in a `merge_paused` notice, a breaker to reset; null otherwise.
 */
export function mergeAsk(notification: Pick<Notification, "notice_kind" | "details" | "project">): MergeAsk | null {
  if (!notification.project) return null;
  if (notification.notice_kind === "merge_paused") return typeof notification.details?.repo === "string" ? "resume" : null;
  if (notification.notice_kind !== "merge_default_branch") return null;
  const ask = notification.details?.ask;
  return ask === "sample" || ask === "release" || ask === "revert" ? ask : null;
}
