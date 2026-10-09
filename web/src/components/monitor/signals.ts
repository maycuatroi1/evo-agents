"use client";

import { useEffect } from "react";

import type { LiveSignal } from "@/components/live/live-model";

/**
 * The connection of each tile of the Monitor, kept outside React state: a tile writes its stream's signal as it
 * changes and takes it back when it closes, and the page reads them all (`useSyncExternalStore`) to tell the top bar
 * the worst. A signal equal to the one kept wakes nobody.
 */
const NONE: readonly LiveSignal[] = [];

export class TileSignals {
  #signals = new Map<string, { json: string; signal: LiveSignal }>();
  #snapshot: readonly LiveSignal[] = NONE;
  #listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  snapshot = (): readonly LiveSignal[] => this.#snapshot;

  serverSnapshot = (): readonly LiveSignal[] => NONE;

  set(key: string, signal: LiveSignal | null): void {
    if (signal === null) {
      if (!this.#signals.delete(key)) return;
    } else {
      const json = JSON.stringify(signal);
      if (this.#signals.get(key)?.json === json) return;
      this.#signals.set(key, { json, signal });
    }
    this.#snapshot = [...this.#signals.values()].map((entry) => entry.signal);
    for (const listener of this.#listeners) listener();
  }
}

/** Keeps the tile's signal in `store` while the tile is shown; null while its stream has nothing to say. */
export function useTileSignal(store: TileSignals, key: string, signal: LiveSignal | null): void {
  const json = signal ? JSON.stringify(signal) : null;
  useEffect(() => {
    store.set(key, json === null ? null : (JSON.parse(json) as LiveSignal));
  }, [store, key, json]);
  useEffect(() => () => store.set(key, null), [store, key]);
}
