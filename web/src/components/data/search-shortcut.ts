"use client";

import { type RefObject, useEffect } from "react";

/**
 * The `/` key of the kit: it moves focus to the search field of the page, unless focus is already in a field that
 * takes text (an input, a text area, a select, an editor or the web terminal) or a dialog is open. Fields register
 * with `useSearchShortcut`; the first one in document order that is shown and not hidden behind a modal wins, so a
 * page with two search fields focuses the one at the top.
 */

const fields = new Set<RefObject<HTMLInputElement | null>>();

/** Input types that do not take typed text: "/" pressed on them still goes to the search field. */
const NOT_TEXT = new Set(["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"]);

export function takesText(target: EventTarget | Element | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target instanceof HTMLInputElement) return !NOT_TEXT.has(target.type);
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLElement && target.isContentEditable) return true;
  const role = target.getAttribute("role");
  return role === "textbox" || role === "combobox" || role === "searchbox";
}

function inModal(element: Element | null): boolean {
  return Boolean(element?.closest('[role="dialog"], [role="alertdialog"]'));
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
  if (event.key !== "/" || event.defaultPrevented || event.isComposing) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const active = document.activeElement;
  if (takesText(event.target) || takesText(active) || inModal(active)) return;
  if (document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]')) return;
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
