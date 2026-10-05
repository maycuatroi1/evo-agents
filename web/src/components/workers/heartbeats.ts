/**
 * The heartbeat strip of a worker's page: one cell per minute of the last hour. The hub keeps only a worker's latest
 * heartbeat (`last_heartbeat_at`), not a history, so the strip is built from what this browser saw: every answer of
 * the workers list or a worker's page is an observation (when it was read, and the latest heartbeat it named).
 *
 * From an observation read at `at` naming the heartbeat `last`, two things are known: a heartbeat arrived in the
 * minute holding `last`, and none arrived between `last` and `at`. A minute is "received" when a heartbeat is known
 * in it, "missed" when it lies inside such a silence longer than a minute, "before" when it ended before the worker
 * registered, and "unknown" otherwise (nobody was looking). The observations live in this tab's memory, copied to
 * sessionStorage so a reload keeps them; nothing reaches the hub.
 */

export const STRIP_MINUTES = 60;
const MINUTE = 60_000;
/** A silence shorter than this is the time between two heartbeats (15 s), not a missed one. */
const SILENCE = MINUTE;
const KEEP = (STRIP_MINUTES + 10) * MINUTE;
const MAX_OBSERVATIONS = 1_000; // per worker: a read every 10 s for 70 minutes is about 420
const STORAGE_KEY = "evo-hub:worker-heartbeats";

export type Observation = { at: number; last: number | null };
export type CellState = "received" | "missed" | "unknown" | "before";
export type Cell = { start: number; state: CellState };

/** The cells of the `minutes` whole minutes up to and including the one holding `now`, oldest first. */
export function heartbeatCells(
  observations: readonly Observation[],
  createdAt: number,
  now: number,
  minutes = STRIP_MINUTES,
): Cell[] {
  const current = Math.floor(now / MINUTE) * MINUTE;
  const first = current - (minutes - 1) * MINUTE;
  const received = new Set<number>();
  const silences: [number, number][] = [];
  let knownUntil = Number.NEGATIVE_INFINITY; // the latest read: nothing is known about the time after it
  for (const { at, last } of observations) {
    if (last !== null) received.add(Math.floor(last / MINUTE) * MINUTE);
    const since = last ?? createdAt; // no heartbeat yet: silent since it registered
    if (at - since > SILENCE) silences.push([since, at]);
    knownUntil = Math.max(knownUntil, at);
  }
  const cells: Cell[] = [];
  for (let start = first; start <= current; start += MINUTE) {
    let state: CellState = "unknown";
    if (received.has(start)) state = "received";
    else if (start + MINUTE <= createdAt) state = "before";
    else if (start < knownUntil) {
      // The part of the minute the worker existed in and someone was looking, all inside one silence.
      const from = Math.max(start, createdAt);
      const end = Math.min(start + MINUTE, knownUntil);
      if (silences.some(([since, until]) => since <= from && end <= until)) state = "missed";
    }
    cells.push({ start, state });
  }
  return cells;
}

export type StripCounts = Record<CellState, number>;

export function countCells(cells: readonly Cell[]): StripCounts {
  const counts: StripCounts = { received: 0, missed: 0, unknown: 0, before: 0 };
  for (const cell of cells) counts[cell.state] += 1;
  return counts;
}

/** The first missed minute of each run of missed minutes, and how long the run lasted, newest last. */
export function missedRuns(cells: readonly Cell[]): { start: number; minutes: number }[] {
  const runs: { start: number; minutes: number }[] = [];
  for (const cell of cells) {
    const previous = runs.at(-1);
    if (cell.state !== "missed") continue;
    if (previous && previous.start + previous.minutes * MINUTE === cell.start) previous.minutes += 1;
    else runs.push({ start: cell.start, minutes: 1 });
  }
  return runs;
}

// The observations of this tab.

type Store = Map<number, Observation[]>;

let store: Store | null = null;
let version = 0;
const listeners = new Set<() => void>();

function load(): Store {
  if (store) return store;
  store = new Map();
  try {
    const saved: unknown = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? "{}");
    if (saved && typeof saved === "object") {
      for (const [key, value] of Object.entries(saved as Record<string, unknown>)) {
        const id = Number(key);
        if (!Number.isSafeInteger(id) || !Array.isArray(value)) continue;
        const kept = value.filter(
          (item): item is Observation =>
            typeof item === "object" &&
            item !== null &&
            typeof item.at === "number" &&
            (item.last === null || typeof item.last === "number"),
        );
        if (kept.length) store.set(id, kept.slice(-MAX_OBSERVATIONS));
      }
    }
  } catch {
    // no storage (private window, blocked site data) or a value this code did not write: start empty
  }
  return store;
}

function save(observations: Store): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(observations)));
  } catch {
    // storage full or blocked: the strip still works for this page's life
  }
}

/** Record what one answer of the hub said about these workers, read at `at` (ms since the epoch). */
export function recordHeartbeats(workers: readonly { id: number; last_heartbeat_at: string | null }[], at: number): void {
  if (typeof window === "undefined" || workers.length === 0) return;
  const observations = load();
  let changed = false;
  for (const worker of workers) {
    const last = worker.last_heartbeat_at ? Date.parse(worker.last_heartbeat_at) : null;
    const list = observations.get(worker.id) ?? [];
    const previous = list.at(-1);
    if (previous && previous.at >= at) continue; // the same answer again (a remount), or an older one
    list.push({ at, last: last !== null && Number.isFinite(last) ? last : null });
    const fresh = list.filter((item) => item.at >= at - KEEP).slice(-MAX_OBSERVATIONS);
    observations.set(worker.id, fresh);
    changed = true;
  }
  if (!changed) return;
  version += 1;
  save(observations);
  for (const notify of listeners) notify();
}

export function subscribeHeartbeats(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Changes whenever an observation is recorded; the snapshot useSyncExternalStore compares. */
export function heartbeatsVersion(): number {
  return version;
}

export function observationsOf(id: number): readonly Observation[] {
  if (typeof window === "undefined") return [];
  return load().get(id) ?? [];
}

/** For tests: forget every observation. */
export function resetHeartbeats(): void {
  store = new Map();
  version += 1;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing to clear
  }
}
