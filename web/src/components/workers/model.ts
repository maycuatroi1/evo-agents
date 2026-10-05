import { LABEL, MAX_LABELS, type Worker, WORKER_NAME } from "./queries";

/**
 * How the web reads a worker: its status as people see it, the filters of the list, the summary cards, and the
 * runtimes and checkouts its heartbeat reports. Pure functions, shared by the pages and their tests.
 */

/**
 * A worker's status on the web. The hub says online, offline, draining or revoked (`worker_status` in runs.py); an
 * online worker is busy while it holds a run and idle otherwise.
 */
export const WORKER_VIEWS = ["idle", "busy", "draining", "offline", "revoked"] as const;
export type WorkerView = (typeof WORKER_VIEWS)[number];

export function workerView(worker: Pick<Worker, "status" | "held_runs">): WorkerView {
  if (worker.status === "online") return worker.held_runs > 0 ? "busy" : "idle";
  return worker.status;
}

export function isWorkerView(value: string | null | undefined): value is WorkerView {
  return (WORKER_VIEWS as readonly string[]).includes(value ?? "");
}

// The list's filters, kept in the URL.

export type WorkerFilters = {
  /** null: every worker that is not revoked. */
  status: WorkerView | null;
  q: string;
};

export const NO_FILTERS: WorkerFilters = { status: null, q: "" };
const MAX_QUERY = 100;

export function readFilters(source: { get(name: string): string | null }): WorkerFilters {
  const status = source.get("status");
  return { status: isWorkerView(status) ? status : null, q: (source.get("q") ?? "").trim().slice(0, MAX_QUERY) };
}

export function filtersSearch(filters: WorkerFilters): string {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.q) params.set("q", filters.q);
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** Whether a worker matches the search text: its name, host, owner, labels or projects hold every word. */
export function matchesWorker(worker: Worker, text: string): boolean {
  const haystack = [worker.name, worker.hostname, worker.owner, worker.os, worker.arch, ...worker.labels, ...worker.projects]
    .join(" ")
    .toLocaleLowerCase();
  return text
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

export function inFacet(worker: Worker, status: WorkerView | null): boolean {
  const view = workerView(worker);
  return status === null ? view !== "revoked" : view === status;
}

export function filterWorkers(workers: Worker[], filters: WorkerFilters): Worker[] {
  return workers.filter((worker) => inFacet(worker, filters.status) && matchesWorker(worker, filters.q));
}

export function countByView(workers: Worker[]): Record<WorkerView, number> {
  const counts: Record<WorkerView, number> = { idle: 0, busy: 0, draining: 0, offline: 0, revoked: 0 };
  for (const worker of workers) counts[workerView(worker)] += 1;
  return counts;
}

// The summary cards.

export type WorkerSummary = Record<WorkerView, number> & {
  /** Workers that are not revoked. */
  live: number;
  /** Runs the busy workers hold, and how many workers that is. */
  heldRuns: number;
  /** Slots an idle or busy worker could still fill, out of all their slots. */
  freeSlots: number;
  totalSlots: number;
  /** The oldest last heartbeat among offline workers, ISO 8601; null when none of them ever sent one. */
  oldestOffline: string | null;
  /** Offline workers that never sent a heartbeat. */
  neverSeen: number;
};

export function summarize(workers: Worker[]): WorkerSummary {
  const counts = countByView(workers);
  let heldRuns = 0;
  let freeSlots = 0;
  let totalSlots = 0;
  let oldestOffline: string | null = null;
  let neverSeen = 0;
  for (const worker of workers) {
    const view = workerView(worker);
    if (view === "idle" || view === "busy") {
      heldRuns += worker.held_runs;
      totalSlots += worker.slots;
      freeSlots += Math.max(worker.slots - worker.held_runs, 0);
    }
    if (view === "offline") {
      if (!worker.last_heartbeat_at) neverSeen += 1;
      else if (!oldestOffline || Date.parse(worker.last_heartbeat_at) < Date.parse(oldestOffline)) {
        oldestOffline = worker.last_heartbeat_at;
      }
    }
  }
  return { ...counts, live: workers.length - counts.revoked, heldRuns, freeSlots, totalSlots, oldestOffline, neverSeen };
}

// What a heartbeat reports. The API keeps these as JSON objects whose shape the daemon sets, so they are read
// defensively: a value may be true or false, a version string, or an object with a few known fields.

export const KNOWN_RUNTIMES: Record<string, string> = { "claude-code": "Claude Code", opencode: "opencode", codex: "Codex CLI" };
const RUNTIME_ORDER = Object.keys(KNOWN_RUNTIMES);
const MAX_TEXT = 200;

export type RuntimeInfo = {
  key: string;
  name: string;
  /** null when the report does not say. */
  available: boolean | null;
  version: string | null;
  detail: string | null;
};

export type CheckoutInfo = { name: string; path: string | null; branch: string | null; detail: string | null };

type Fields = Record<string, unknown>;

function isFields(value: unknown): value is Fields {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  return trimmed.length > MAX_TEXT ? `${trimmed.slice(0, MAX_TEXT)}…` : trimmed;
}

function firstText(fields: Fields, keys: string[]): string | null {
  for (const key of keys) {
    const found = text(fields[key]);
    if (found) return found;
  }
  return null;
}

function flag(fields: Fields, keys: string[]): boolean | null {
  for (const key of keys) if (typeof fields[key] === "boolean") return fields[key] as boolean;
  return null;
}

function runtime(key: string, value: unknown): RuntimeInfo {
  const name = KNOWN_RUNTIMES[key] ?? key;
  if (typeof value === "boolean") return { key, name, available: value, version: null, detail: null };
  if (value === null || value === undefined) return { key, name, available: false, version: null, detail: null };
  if (!isFields(value)) return { key, name, available: true, version: text(value), detail: null };
  const detail = firstText(value, ["reason", "error", "status", "auth", "path"]);
  const problem = firstText(value, ["reason", "error"]) !== null;
  const available = flag(value, ["available", "ok", "found", "supported"]) ?? (problem ? false : true);
  return { key, name, available, version: firstText(value, ["version"]), detail };
}

export function readRuntimes(runtimes: Record<string, unknown>): RuntimeInfo[] {
  const rank = (key: string) => (RUNTIME_ORDER.includes(key) ? RUNTIME_ORDER.indexOf(key) : RUNTIME_ORDER.length);
  return Object.entries(runtimes)
    .map(([key, value]) => runtime(key, value))
    .sort((a, b) => rank(a.key) - rank(b.key) || a.name.localeCompare(b.name));
}

export function readCheckouts(checkouts: Record<string, unknown>): CheckoutInfo[] {
  return Object.entries(checkouts)
    .map(([name, value]): CheckoutInfo => {
      if (Array.isArray(value)) {
        const paths = value.map(text).filter((path): path is string => path !== null);
        return { name, path: paths.join(", ") || null, branch: null, detail: null };
      }
      if (!isFields(value)) return { name, path: text(value), branch: null, detail: null };
      return {
        name,
        path: firstText(value, ["path", "dir", "root"]),
        branch: firstText(value, ["branch", "head"]),
        detail: firstText(value, ["origin", "remote", "status"]),
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

// The registration form.

export type LabelsInput = { labels: string[]; invalid: string[]; tooMany: boolean };

/** Labels typed as a comma or space separated list: unique, in order, each checked against the API's pattern. */
export function parseLabels(input: string): LabelsInput {
  const words = [...new Set(input.split(/[\s,]+/).map((word) => word.trim()).filter(Boolean))];
  const invalid = words.filter((word) => !LABEL.test(word));
  const labels = words.filter((word) => LABEL.test(word));
  return { labels, invalid, tooMany: labels.length > MAX_LABELS };
}

export function isWorkerName(name: string): boolean {
  return WORKER_NAME.test(name);
}

/** The commands the dialog shows, built from what the person typed. Names and labels match the API's patterns,
 * which leave nothing a shell would read specially, so no quoting is needed. */
export const INSTALL_COMMAND = "uv tool install 'evo-ak[worker]'";
export const SERVICE_COMMAND = "evo-agents worker service install";

export function joinCommand(hubUrl: string, code: string): string {
  return `evo-agents worker join --url ${hubUrl} --code ${code}`;
}

export function registerCommand(input: { name: string; projects: string[]; slots: number; labels: string[] }): string {
  const parts = ["evo-agents worker register", `--name ${isWorkerName(input.name) ? input.name : '"$(hostname -s)"'}`];
  for (const project of input.projects) parts.push(`--project ${project}`);
  parts.push(`--slots ${input.slots}`);
  for (const label of input.labels) parts.push(`--label ${label}`);
  return parts.join(" ");
}
