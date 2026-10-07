"use client";

import { type RefObject, useEffect } from "react";

import { characterKeyBlocked } from "@/lib/keyboard";

/**
 * The `/` key of the kit: it moves focus to the search field of the page, unless focus is already where keys are
 * typed (an input, a text area, a select, an editor, the web terminal, a menu) or a dialog is open, or the visitor
 * turned single-key shortcuts off (`characterKeyBlocked` in lib/keyboard.ts, shared with the shell's other single
 * keys in components/shell/shortcuts.tsx). Fields register with `useSearchShortcut`; the first one in document order
 * that is shown and not hidden behind a modal wins, so a page with two search fields focuses the one at the top.
 */

export { takesText } from "@/lib/keyboard";

const fields = new Set<RefObject<HTMLInputElement | null>>();

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

/** Registers a search field for the `/` key while it is mounted. */
export function useSearchShortcut(ref: RefObject<HTMLInputElement | null>, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    fields.add(ref);
    if (fields.size === 1) document.addEventListener("keydown", onKeyDown);
    return () => {
      fields.delete(ref);
      if (fields.size === 0) document.removeEventListener("keydown", onKeyDown);
    };
  }, [ref, enabled]);
}
