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

/** The daemon's last error stands: no later check of nightly found it on the commit nightly points to. */
function lastUpdateFailed(report: SelfUpdate | null | undefined): boolean {
  return Boolean(report?.error && !(report.state === "current" && later(report.checked_at, report.error_at)));
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
  if (lastUpdateFailed(report)) found.push("failed");
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

/**
 * The hub reads where nightly points once a minute and shows that read on every worker. A read older than this means
 * the hub has not reached GitHub since; a daemon then reads the tag itself, so the page says so (docs/workers.md).
 */
export const HUB_READ_FRESH_MS = 10 * 60 * 1000;

/** The feature a daemon says when it carries out its owner's Update now (`runs.UPDATE_NOW` of the hub). */
export const UPDATE_NOW_FEATURE = "update_now";

type Nightly = NonNullable<Worker["nightly"]>;

/** The hub's read of nightly when it is over 10 minutes old by `now`; null while it is fresh, absent or `now` unknown. */
export function staleNightly(nightly: Nightly | null, now: number | null): Nightly | null {
  if (!nightly || now === null) return null;
  const read = Date.parse(nightly.read_at);
  return Number.isFinite(read) && now - read > HUB_READ_FRESH_MS ? nightly : null;
}

/** The worker's Update now while it is open: asked for and not yet ended. */
export function openUpdateNow(worker: Pick<Worker, "update_now">): Worker["update_now"] {
  const request = worker.update_now;
  return request && request.ended_at === null ? request : null;
}

/**
 * What the page offers of Update now. `hidden`: nothing to offer, the worker runs nightly or is revoked. `open`: a request
 * waits. `ready`: the worker runs another commit than nightly, or its last update failed, and the owner may ask. `blocked`:
 * it would be asked for, but cannot happen; `why` says what stands in the way, as the hub would refuse it.
 */
export type UpdateNowOffer =
  | { kind: "hidden" }
  | { kind: "open" }
  | { kind: "ready" }
  | { kind: "blocked"; why: "locked" | "unsupported" | "tooOld" };

type Offered = Pick<Worker, "status" | "version_locked" | "self_update" | "update_now" | "nightly" | "agent_commit" | "features">;

/** The commit nightly points to: the hub's read, else what the daemon itself last read (a hub that has not read it yet). */
export function nightlyCommit(worker: Pick<Worker, "nightly" | "self_update">): string | null {
  return worker.nightly?.commit ?? worker.self_update?.nightly_commit ?? null;
}

export function updateNowOffer(worker: Offered): UpdateNowOffer {
  if (worker.status === "revoked") return { kind: "hidden" };
  if (openUpdateNow(worker)) return { kind: "open" };
  const report = worker.self_update;
  const nightly = nightlyCommit(worker);
  const behind = Boolean(worker.agent_commit && nightly && worker.agent_commit !== nightly);
  // The hub refuses a worker on the commit it read, whatever an earlier attempt left in the report.
  if (worker.nightly && worker.agent_commit === worker.nightly.commit) return { kind: "hidden" };
  if (!behind && !lastUpdateFailed(report)) return { kind: "hidden" };
  if (worker.version_locked) return { kind: "blocked", why: "locked" };
  if (report && (report.state === "unsupported" || report.state === "locked")) return { kind: "blocked", why: "unsupported" };
  if (!(worker.features ?? []).includes(UPDATE_NOW_FEATURE)) return { kind: "blocked", why: "tooOld" };
  return { kind: "ready" };
}

/**
 * What an open request waits for, the first that holds: `offline`, the worker to come back; `installing`, the daemon to
 * restart on nightly; `runs`, the runs it holds to end (it claims no new one); `asked`, its next heartbeat to take the
 * request; `starting`, the daemon took it and updates once every daemon of its install is idle.
 */
export type UpdateNowPhase = "offline" | "installing" | "runs" | "asked" | "starting";

export function updateNowPhase(worker: Pick<Worker, "status" | "self_update" | "held_runs" | "update_now">): UpdateNowPhase {
  if (worker.status === "offline") return "offline";
  if (worker.self_update?.state === "updating") return "installing";
  if (worker.held_runs > 0) return "runs";
  const taken = worker.self_update?.update_now?.requested_at;
  const asked = worker.update_now?.requested_at;
  return taken && asked && Date.parse(taken) === Date.parse(asked) ? "starting" : "asked";
}
