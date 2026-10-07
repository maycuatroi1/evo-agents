import { useSyncExternalStore } from "react";

/**
 * The key that goes with Enter to send a form, as the visitor's keyboard labels it: Command on Apple devices, Ctrl
 * elsewhere. Null on the server and during hydration, so the two renders agree and the hint appears once the browser
 * knows; the shortcut itself works either way, as a form takes Meta or Ctrl with Enter.
 */
export type ModifierKey = "meta" | "ctrl";

const APPLE = /Mac|iPhone|iPad|iPod/i;

function platform(): string {
  const data = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  return data?.platform || navigator.platform || navigator.userAgent;
}

const subscribe = () => () => {};
const client = (): ModifierKey => (APPLE.test(platform()) ? "meta" : "ctrl");
const server = (): ModifierKey | null => null;

export function useModifierKey(): ModifierKey | null {
  return useSyncExternalStore(subscribe, client, server);
}

/** Whether a key press is the send shortcut: Enter with Command or Ctrl, nothing else held and not mid-composition. */
export function isSendShortcut(event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "isComposing">): boolean {
  return event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && !event.isComposing;
}

/**
 * A key of a shortcut as the registry writes it: `mod` is Command on Apple devices and Ctrl elsewhere, `enter` and
 * `esc` are those keys, anything else is the character on the key.
 */
export type KeyPart = "mod" | "enter" | "esc" | (string & {});

/** One key as a `kbd` shows it: ⌘ or Ctrl, ↵, Esc, a letter in capitals. */
export function keyText(part: KeyPart, modifier: ModifierKey | null): string {
  if (part === "mod") return modifier === "meta" ? "⌘" : "Ctrl";
  if (part === "enter") return "↵";
  if (part === "esc") return "Esc";
  return part.toUpperCase();
}

/**
 * A chord written in one `kbd`, as the top bar's field and the send buttons show theirs: "⌘K" and "⌘↵" on Apple
 * devices (the symbol and the key run together), "Ctrl K" and "Ctrl ↵" elsewhere.
 */
export function comboText(parts: readonly KeyPart[], modifier: ModifierKey | null): string {
  const texts = parts.map((part) => keyText(part, modifier));
  return modifier === "meta" ? texts.join("") : texts.join(" ");
}

/** Input types that do not take typed text: a key pressed on them is still a shortcut. */
const NOT_TEXT = new Set(["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"]);

const EDITABLE = '[contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]';

/** Whether `target` takes typed text: a text input, a text area, a select, an editor, or a textbox by its role. */
export function takesText(target: EventTarget | Element | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target instanceof HTMLInputElement) return !NOT_TEXT.has(target.type);
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  // isContentEditable, or the attribute itself where an engine does not compute it (jsdom).
  if (target instanceof HTMLElement && (target.isContentEditable || target.closest(EDITABLE) !== null)) return true;
  const role = target.getAttribute("role");
  return role === "textbox" || role === "combobox" || role === "searchbox";
}

/**
 * Where a single key belongs to what has focus: the web terminal (every key goes to the agent's TUI), a dialog, a menu
 * or a list that moves by typed letters, and a widget that takes the keyboard for itself (`role="application"`, the
 * knowledge graph's canvas).
 */
const OWNS_KEYS = [
  ".hub-terminal",
  '[role="dialog"]',
  '[role="alertdialog"]',
  '[role="menu"]',
  '[role="menubar"]',
  '[role="listbox"]',
  '[role="tree"]',
  '[role="grid"]',
  '[role="application"]',
].join(", ");

/** A dialog, sheet or popover open on the page: single keys leave the page behind it alone. */
const OPEN_OVERLAY = '[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]';

function ownsKeys(target: EventTarget | Element | null): boolean {
  if (takesText(target)) return true;
  return target instanceof Element && target.closest(OWNS_KEYS) !== null;
}

/** The single-key shortcuts (/, ?, G then a letter, D): on unless the visitor turned them off on this browser. */
const CHARACTER_KEYS = "hub.shortcuts.characterKeys";

const listeners = new Set<() => void>();

/** The choice when storage refused it, for the rest of the page's life. */
let memory: boolean | null = null;

function readCharacterKeys(): boolean {
  if (memory !== null) return memory;
  try {
    return window.localStorage.getItem(CHARACTER_KEYS) !== "off";
  } catch {
    return true; // storage refused (a private window, blocked site data): the keys stay on
  }
}

function subscribeCharacterKeys(listener: () => void) {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === CHARACTER_KEYS || event.key === null) listener();
  };
  window.addEventListener("storage", onStorage); // another tab changed it
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** Whether the single-key shortcuts are on, read when a key is pressed. */
export function characterKeysOn(): boolean {
  return typeof window === "undefined" ? true : readCharacterKeys();
}

/**
 * Turns the single-key shortcuts on or off on this browser (WCAG 2.1.4, Character Key Shortcuts): speech input and
 * some switch devices type letters a page must not take as commands. Shortcuts with Cmd or Ctrl stay.
 */
export function setCharacterKeys(on: boolean): void {
  try {
    if (on) window.localStorage.removeItem(CHARACTER_KEYS);
    else window.localStorage.setItem(CHARACTER_KEYS, "off");
  } catch {
    memory = on; // storage refused: the choice lasts until the page reloads
  }
  for (const listener of listeners) listener();
}

/** Whether the single-key shortcuts are on; on during the server render and hydration. */
export function useCharacterKeys(): boolean {
  return useSyncExternalStore(subscribeCharacterKeys, readCharacterKeys, () => true);
}

/**
 * Whether a single-key shortcut must leave this key press alone: a modifier is held (Cmd, Ctrl or Alt, the browser's
 * and the screen reader's own keys), the key repeats or is part of a composition, the visitor turned single keys off,
 * focus is where keys are typed or owned (a text field, the terminal, a menu, a dialog), or a dialog is open. Shift
 * is the caller's to judge: `?` needs it on most layouts, a letter does not take it.
 */
export function characterKeyBlocked(
  event: Pick<KeyboardEvent, "defaultPrevented" | "isComposing" | "repeat" | "ctrlKey" | "metaKey" | "altKey" | "target">,
): boolean {
  if (event.defaultPrevented || event.isComposing || event.repeat) return true;
  if (event.ctrlKey || event.metaKey || event.altKey) return true;
  if (!characterKeysOn()) return true;
  if (ownsKeys(event.target) || ownsKeys(document.activeElement)) return true;
  return document.querySelector(OPEN_OVERLAY) !== null;
}
