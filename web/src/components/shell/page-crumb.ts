"use client";

import { useEffect, useSyncExternalStore } from "react";

/**
 * The name a page gives its own crumb, in place of the last part of its path: a use case's code rather than its key.
 * One page shows at a time, so one name is kept, with the path it names; the top bar reads it for that path only, and
 * the page takes it back when it leaves.
 */
type Named = { path: string; label: string };

let current: Named | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The page at `path` names its crumb `label` while it shows; null names nothing. */
export function usePageCrumb(path: string, label: string | null): void {
  useEffect(() => {
    if (!label) return;
    current = { path, label };
    emit();
    return () => {
      if (current?.path === path && current.label === label) {
        current = null;
        emit();
      }
    };
  }, [path, label]);
}

/** The name the page at `path` gave its crumb, or null. */
export function usePageCrumbLabel(path: string): string | null {
  const named = useSyncExternalStore(
    subscribe,
    () => current,
    () => null,
  );
  return named && named.path === path ? named.label : null;
}
