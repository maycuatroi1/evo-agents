/**
 * The pages' own Dispatch, which D opens instead of the shell's (components/shell/shortcuts.tsx): each page that has
 * one registers it under its `useId` while it is mounted, and D opens the one registered last. A page registers in
 * an effect, once its content has hydrated, which can come well after the shell's own keys work; until then D opens
 * the shell's Dispatch. The registry is a store outside React, so a page can show the key beside its button only from
 * the moment D opens that button's dialog (`useDispatchShortcut`).
 */

let entries: readonly { id: string; open: () => void }[] = [];
const listeners = new Set<() => void>();

function changed() {
  for (const listener of listeners) listener();
}

/** The Dispatch D opens now: the page's own registered last, or none. */
export function pageDispatch(): (() => void) | undefined {
  return entries.at(-1)?.open;
}

/** Registers a page's Dispatch under `id`; returns its removal. */
export function registerPageDispatch(id: string, open: () => void): () => void {
  entries = [...entries, { id, open }];
  changed();
  return () => {
    entries = entries.filter((entry) => entry.id !== id);
    changed();
  };
}

export function isPageDispatchRegistered(id: string): boolean {
  return entries.some((entry) => entry.id === id);
}

export function subscribePageDispatch(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
