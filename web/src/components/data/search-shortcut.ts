"use client";

import { type RefObject, useEffect, useSyncExternalStore } from "react";

import { characterKeyBlocked, useCharacterKeys } from "@/lib/keyboard";

/**
 * The `/` key of the kit: it moves focus to the search field of the page, unless focus is already where keys are
 * typed (an input, a text area, a select, an editor, the web terminal, a menu) or a dialog is open, or the visitor
 * turned single-key shortcuts off (`characterKeyBlocked` in lib/keyboard.ts, shared with the shell's other single
 * keys in components/shell/shortcuts.tsx). Fields register with `useSearchShortcut`; the first one in document order
 * that is shown and not hidden behind a modal wins, so a page with two search fields focuses the one at the top.
 *
 * A field registers once its page has hydrated, which can come well after the shell's own keys work: the page's
 * content streams in its own Suspense boundary. Until then `/` does nothing, so the field shows and announces the key
 * only from that moment (`useSearchShortcut` says when), as the top bar's field shows its key only after hydration.
 */

export { takesText } from "@/lib/keyboard";

const fields = new Set<RefObject<HTMLInputElement | null>>();

/** The fields that read the registry, told when a field joins or leaves it. */
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function changed() {
  for (const listener of listeners) listener();
}

function usable(input: HTMLInputElement): boolean {
  if (!input.isConnected || input.disabled || input.readOnly) return false;
  // A modal hides the rest of the page from assistive technology (aria-hidden or inert); a field there is out of reach.
  if (input.closest('[aria-hidden="true"], [inert]')) return false;
  return input.getClientRects().length > 0;
}

/** The field "/" focuses now: the first registered one in document order that can take focus. */
export function shortcutTarget(): HTMLInputElement | null {
  const inputs = [...fields]
    .map((ref) => ref.current)
    .filter((input): input is HTMLInputElement => input !== null && usable(input));
  inputs.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  return inputs[0] ?? null;
}

function onKeyDown(event: KeyboardEvent) {
  if (event.key !== "/" || characterKeyBlocked(event)) return;
  const input = shortcutTarget();
  if (!input) return;
  event.preventDefault(); // the slash is not typed into the field
  input.focus();
  input.select();
}

/**
 * Registers a search field for the `/` key while it is mounted, and returns whether the key reaches it now: the field
 * is registered (never during the server render and hydration) and single-key shortcuts are on. The field shows its
 * `kbd` and sets `aria-keyshortcuts` only while this is true, so neither promises a key that would do nothing.
 */
export function useSearchShortcut(ref: RefObject<HTMLInputElement | null>, enabled = true): boolean {
  const registered = useSyncExternalStore(subscribe, () => fields.has(ref), () => false);
  const characterKeys = useCharacterKeys();
  useEffect(() => {
    if (!enabled) return;
    fields.add(ref);
    if (fields.size === 1) document.addEventListener("keydown", onKeyDown);
    changed();
    return () => {
      fields.delete(ref);
      if (fields.size === 0) document.removeEventListener("keydown", onKeyDown);
      changed();
    };
  }, [ref, enabled]);
  return enabled && registered && characterKeys;
}
