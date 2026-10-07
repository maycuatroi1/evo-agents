"use client";

import { hashKey, type Query, type QueryKey } from "@tanstack/react-query";
import { createContext, type ReactNode, useContext, useEffect, useId, useRef, useState } from "react";

import type { LiveSignal } from "./live-model";

/**
 * Where a page tells the top bar what keeps it current. A page registers its main query (`useLiveQuery`, done by
 * `useHubQuery` with `live`), and a page with a stream registers the stream's signal (`useLiveSignal`); the top bar's
 * LiveIndicator reads them. Registrations end with the component that made them, so a page left behind leaves
 * nothing in the top bar. Without a provider (a test of one component) registering does nothing.
 */
type RefetchInterval = number | false | undefined | ((query: Query<never, never, never, never>) => number | false | undefined);

export type LiveQueryEntry = {
  kind: "query";
  id: string;
  queryKey: QueryKey;
  queryHash: string;
  /** The query's refetchInterval as the page last gave it: a number of ms or a function of the query. */
  interval: () => RefetchInterval;
};

export type LiveSignalEntry = {
  kind: "signal";
  id: string;
  signal: LiveSignal;
  /** Resumes what the person paused (the log's Pause). */
  resume?: () => void;
  /** Tries again at once (Retry now). */
  retry?: () => void;
};

export type LiveEntry = LiveQueryEntry | LiveSignalEntry;

export class LiveStore {
  #entries = new Map<string, LiveEntry>();
  #snapshot: readonly LiveEntry[] = [];
  #listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  /** The entries, signals (a stream, the page's main transport) before queries; the same array until one changes. */
  snapshot = (): readonly LiveEntry[] => this.#snapshot;

  set(entry: LiveEntry): void {
    this.#entries.set(entry.id, entry);
    this.#emit();
  }

  delete(id: string): void {
    if (this.#entries.delete(id)) this.#emit();
  }

  #emit(): void {
    const all = [...this.#entries.values()];
    this.#snapshot = [...all.filter((entry) => entry.kind === "signal"), ...all.filter((entry) => entry.kind === "query")];
    for (const listener of this.#listeners) listener();
  }
}

const LiveContext = createContext<LiveStore | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [store] = useState(() => new LiveStore());
  return <LiveContext.Provider value={store}>{children}</LiveContext.Provider>;
}

export function useLiveStore(): LiveStore | null {
  return useContext(LiveContext);
}

/** Registers a polling query as what keeps the page current, while `enabled`. */
export function useLiveQuery(options: { queryKey: QueryKey; refetchInterval?: unknown }, enabled: boolean): void {
  const store = useLiveStore();
  const id = useId();
  const interval = useRef<RefetchInterval>(undefined);
  useEffect(() => {
    interval.current = options.refetchInterval as RefetchInterval;
  });
  const queryHash = hashKey(options.queryKey);
  const queryKey = useRef(options.queryKey);
  useEffect(() => {
    queryKey.current = options.queryKey;
  });
  useEffect(() => {
    if (!store || !enabled) return;
    store.set({ kind: "query", id, queryKey: queryKey.current, queryHash, interval: () => interval.current });
    return () => store.delete(id);
  }, [store, id, enabled, queryHash]);
}

/**
 * Registers a stream's signal (null while the stream is not what keeps the page current), with what Resume and Retry
 * now do. The signal is compared by value, so a render that changes nothing does not wake the top bar.
 */
export function useLiveSignal(signal: LiveSignal | null, actions: { resume?: () => void; retry?: () => void } = {}): void {
  const store = useLiveStore();
  const id = useId();
  const handlers = useRef(actions);
  useEffect(() => {
    handlers.current = actions;
  });
  const key = signal ? JSON.stringify(signal) : null;
  useEffect(() => {
    if (!store || key === null) return;
    store.set({
      kind: "signal",
      id,
      signal: JSON.parse(key) as LiveSignal,
      resume: () => handlers.current.resume?.(),
      retry: () => handlers.current.retry?.(),
    });
  }, [store, id, key]);
  useEffect(() => {
    if (!store || key !== null) return;
    store.delete(id);
  }, [store, id, key]);
  useEffect(() => () => store?.delete(id), [store, id]);
}
