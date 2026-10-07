import type { QueryState } from "@tanstack/react-query";

/**
 * Whether what a page shows is current (the kit's LiveIndicator, web/DESIGN.md): Live while its updates arrive,
 * Reconnecting while they fail or come by a fallback, Paused while the person holds them (the log's Pause), and
 * Offline once they have failed for OFFLINE_AFTER_MS or the browser has no network. Offline holds until an update
 * arrives again, so a failing page does not flicker between Reconnecting and Offline at every try.
 *
 * Each source of updates (a polling query, a run's event stream) is reduced to a LiveSignal; the indicator merges the
 * page's signals and steps the machine on every signal and every tick of the shared clock.
 */
export type LiveState = "live" | "reconnecting" | "paused" | "offline";

export type LiveSignal = {
  /** How the updates come: a stream the hub pushes, or reads the page repeats. */
  transport: "stream" | "poll";
  /** When the page last received fresh data (ms since the epoch); 0 before the first. */
  updatedAt: number;
  /** The last attempt failed and none has succeeded since. */
  failing: boolean;
  /** Updates still arrive, by a fallback (the run log reads events while its stream is retried). */
  degraded: boolean;
  /** The browser reports no network: no attempt is made until it is back. */
  offline: boolean;
  /** The person paused the updates. */
  paused: boolean;
  /** When the next attempt is due (ms since the epoch); null when it is under way or unknown. */
  retryAt: number | null;
  /** How often the page reads meanwhile (ms); null when it does not poll. */
  pollMs: number | null;
};

export type LiveMachine = {
  state: LiveState;
  /** When the current run of failures began; null while nothing fails. */
  failingSince: number | null;
};

/** How long updates may fail before the page says Offline. */
export const OFFLINE_AFTER_MS = 15_000;

export const INITIAL_LIVE: LiveMachine = { state: "live", failingSince: null };

/** One step of the machine: the state for `signal` at `now`, given the state before. */
export function stepLive(previous: LiveMachine, signal: LiveSignal, now: number): LiveMachine {
  if (signal.paused) return previous.state === "paused" && previous.failingSince === null ? previous : { state: "paused", failingSince: null };
  if (signal.failing) {
    const failingSince = previous.failingSince ?? now;
    const state: LiveState =
      signal.offline || previous.state === "offline" || now - failingSince >= OFFLINE_AFTER_MS ? "offline" : "reconnecting";
    return previous.state === state && previous.failingSince === failingSince ? previous : { state, failingSince };
  }
  if (signal.offline) return previous.state === "offline" ? previous : { state: "offline", failingSince: previous.failingSince ?? now };
  const state: LiveState = signal.degraded ? "reconnecting" : "live";
  return previous.state === state && previous.failingSince === null ? previous : { state, failingSince: null };
}

/**
 * A polling query's signal from its state in the cache. A failure counts from the first failed try (TanStack counts
 * the retries of one fetch in fetchFailureCount) until data arrives again (the last error stays newer than the data);
 * a fetch paused by the browser going offline is offline. The next try is due one interval after the last failure,
 * unless one is under way.
 */
export function querySignal(state: QueryState<unknown, unknown>, pollMs: number | null): LiveSignal {
  const failing = state.fetchFailureCount > 0 || state.errorUpdatedAt > state.dataUpdatedAt;
  const trying = state.fetchStatus === "fetching";
  return {
    transport: "poll",
    updatedAt: state.dataUpdatedAt,
    failing,
    degraded: false,
    offline: state.fetchStatus === "paused",
    paused: false,
    retryAt: failing && !trying && pollMs !== null && state.errorUpdatedAt > 0 ? state.errorUpdatedAt + pollMs : null,
    pollMs,
  };
}

/**
 * The page's signals as one: the first is the page's main transport (the run's stream before its polled query), and
 * the page is as fresh as the newest update any of them brought.
 */
export function mergeSignals(signals: readonly LiveSignal[]): LiveSignal | null {
  if (signals.length === 0) return null;
  const [main] = signals;
  return { ...main, updatedAt: Math.max(...signals.map((signal) => signal.updatedAt)) };
}

/** Seconds from now to `at`, never below zero; null when `at` is unknown. */
export function secondsUntil(at: number | null, now: number): number | null {
  return at === null ? null : Math.max(0, Math.ceil((at - now) / 1000));
}

/** Seconds since `at`, never below zero. */
export function secondsSince(at: number, now: number): number {
  return Math.max(0, Math.floor((now - at) / 1000));
}
