import { useSyncExternalStore } from "react";

/**
 * The browser's clock, ticking every second while `active`, for the durations of builds that are still waiting or
 * running. Null on the server and during hydration, so the two renders agree; one timer serves every caller.
 */
const TICK = 1_000;
const listeners = new Set<() => void>();
let current = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    current = Date.now();
    timer = setInterval(() => {
      current = Date.now();
      for (const notify of listeners) notify();
    }, TICK);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const idle = () => () => {};
const snapshot = () => current || (current = Date.now());
const none = () => null;

export function useNow(active: boolean): number | null {
  return useSyncExternalStore(active ? subscribe : idle, active ? snapshot : none, none);
}
