import type { Worker } from "./queries";

/**
 * How the web reads a worker's evo-agents: the commit its daemon runs, its owner's version lock, and what the daemon
 * last said of updating itself to the tag nightly (docs/workers.md). The pages say only what went wrong: an update that
 * failed, a daemon that cannot update itself, or one that has waited for an idle moment far longer than nightly takes to
 * move. Pure functions, shared by the pages and their tests.
 */

export type SelfUpdate = NonNullable<Worker["self_update"]>;

/** The repository every worker installs evo-agents from (the tag nightly of maycuatroi1/evo-agents). */
export const EVO_AGENTS_REPO = "https://github.com/maycuatroi1/evo-agents";

/** `OBJECT_NAME` of the API: a full SHA-1 or SHA-256 object name, lowercase. */
const OBJECT_NAME = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;

/** The commit's page on GitHub; null for anything that is not a full object name, so no other text becomes a link. */
export function commitUrl(sha: string | null | undefined): string | null {
  return sha && OBJECT_NAME.test(sha) ? `${EVO_AGENTS_REPO}/commit/${sha}` : null;
}

/** A commit as people read it: its first 7 characters. */
export function shortCommit(sha: string): string {
  return sha.slice(0, 7);
}

/**
 * How long a daemon may wait for every daemon of its install to be idle before the pages say so. The tag nightly moves
 * once a day and a daemon sees it within about 2 hours; half a day of waiting means its runs never leave it idle.
 */
export const LONG_WAIT_MS = 12 * 60 * 60 * 1000;

/** What went wrong, most serious first. */
export const UPDATE_PROBLEMS = ["failed", "unsupported", "waiting"] as const;
export type UpdateProblem = (typeof UPDATE_PROBLEMS)[number];

type Reported = Pick<Worker, "status" | "version_locked" | "self_update">;

function later(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  const left = Date.parse(a);
  const right = Date.parse(b);
  return Number.isFinite(left) && Number.isFinite(right) && left > right;
}

/** How long the daemon has said `waiting`, in milliseconds; null when the hub has no start or the clock is unknown. */
export function waitedMs(report: SelfUpdate, now: number | null): number | null {
  if (report.state !== "waiting" || !report.since || now === null) return null;
  const since = Date.parse(report.since);
  return Number.isFinite(since) ? Math.max(now - since, 0) : null;
}

/**
 * What went wrong with a worker's updates, most serious first; empty when nothing did. A revoked worker, a worker its
 * owner locked, and a daemon that says nothing of updating itself (an older one) have nothing to report. An error stays
 * until a later check of nightly found the daemon on it; a long wait counts only while the worker sends heartbeats, and
 * only once `now` is known (the browser's clock, null while the page hydrates).
 */
export function updateProblems(worker: Reported, now: number | null): UpdateProblem[] {
  const report = worker.self_update;
  if (worker.status === "revoked" || worker.version_locked || !report || report.state === "locked") return [];
  const found: UpdateProblem[] = [];
  if (report.error && !(report.state === "current" && later(report.checked_at, report.error_at))) found.push("failed");
  if (report.state === "unsupported") found.push("unsupported");
  const waited = worker.status === "offline" ? null : waitedMs(report, now);
  if (waited !== null && waited >= LONG_WAIT_MS) found.push("waiting");
  return found;
}

/**
 * What the Version card says of updates, one word each: `none` before the first heartbeat, `locked` while the owner keeps
 * the version, `unlocking` until the daemon takes an unlock, `unreported` for a daemon that says nothing of it, else the
 * daemon's state, and `other` for a state this page does not know (the hub keeps a newer name as sent).
 */
export const UPDATE_STATES = ["none", "locked", "unlocking", "unreported", "current", "waiting", "updating", "unsupported", "other"] as const;
export type UpdateState = (typeof UPDATE_STATES)[number];

const DAEMON_STATES: readonly string[] = ["current", "waiting", "updating", "unsupported"];

export function updateState(worker: Pick<Worker, "version_locked" | "self_update" | "last_heartbeat_at">): UpdateState {
  if (worker.version_locked) return "locked";
  const report = worker.self_update;
  if (!report) return worker.last_heartbeat_at ? "unreported" : "none";
  if (report.state === "locked") return "unlocking";
  return DAEMON_STATES.includes(report.state) ? (report.state as UpdateState) : "other";
}
