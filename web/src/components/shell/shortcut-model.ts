"use client";

import type { Route } from "next";

import { characterKeyBlocked, comboText, type KeyPart, type ModifierKey } from "@/lib/keyboard";

import { type NavLabel } from "./nav";

export type ShortcutId =
  | "palette" | "search" | "help" | "sidebar" | "close"
  | "goHome" | "goInbox" | "goPlans" | "goRuns" | "goCurator" | "goWorkers"
  | "dispatch" | "send" | "chatSend" | "chatLine" | "nextItem" | "previousItem";

type ShortcutGroup = "general" | "goto" | "actions";

export type Shortcut = {
  id: ShortcutId;
  group: ShortcutGroup;
  /** Keys pressed together; a second entry is pressed after the first (G then H). */
  keys: readonly (readonly KeyPart[])[];
  /** A key pressed alone: off while the visitor has single-key shortcuts off. */
  single: boolean;
  /** Where it works, when that is not every page: said under its name in the dialog. */
  where?: "project" | "writer" | "chat" | "inbox";
};

export const SHORTCUTS: readonly Shortcut[] = [
  { id: "palette", group: "general", keys: [["mod", "k"]], single: false },
  { id: "search", group: "general", keys: [["/"]], single: true },
  { id: "help", group: "general", keys: [["?"]], single: true },
  { id: "sidebar", group: "general", keys: [["mod", "b"]], single: false },
  { id: "close", group: "general", keys: [["esc"]], single: false },
  { id: "dispatch", group: "actions", keys: [["d"]], single: true, where: "writer" },
  { id: "send", group: "actions", keys: [["mod", "enter"]], single: false },
  { id: "chatSend", group: "actions", keys: [["enter"]], single: false, where: "chat" },
  { id: "chatLine", group: "actions", keys: [["shift", "enter"]], single: false, where: "chat" },
  { id: "nextItem", group: "actions", keys: [["j"]], single: true, where: "inbox" },
  { id: "previousItem", group: "actions", keys: [["k"]], single: true, where: "inbox" },
  { id: "goHome", group: "goto", keys: [["g"], ["h"]], single: true },
  { id: "goInbox", group: "goto", keys: [["g"], ["i"]], single: true },
  { id: "goPlans", group: "goto", keys: [["g"], ["p"]], single: true, where: "project" },
  { id: "goRuns", group: "goto", keys: [["g"], ["r"]], single: true, where: "project" },
  { id: "goCurator", group: "goto", keys: [["g"], ["c"]], single: true, where: "project" },
  { id: "goWorkers", group: "goto", keys: [["g"], ["w"]], single: true },
];

export const BY_ID = Object.fromEntries(SHORTCUTS.map((entry) => [entry.id, entry])) as Record<ShortcutId, Shortcut>;

/** The dialog's groups, in two columns from 768 px: General and Actions, then Go to. */
export const COLUMNS: readonly (readonly ShortcutGroup[])[] = [["general", "actions"], ["goto"]];

/** The keys of a shortcut as one `kbd` each stroke: "⌘K" or "Ctrl K"; "G", "H". */
export function strokeTexts(id: ShortcutId, modifier: ModifierKey | null): string[] {
  return BY_ID[id].keys.map((stroke) => comboText(stroke, modifier));
}

/** A chord's keys as the one `kbd` beside its action shows them: "⌘K" on Apple devices, "Ctrl K" elsewhere. */
export function shortcutText(id: ShortcutId, modifier: ModifierKey | null): string {
  return strokeTexts(id, modifier).join(" ");
}

/** The key that starts a "go to" sequence, and how long the second key may take. */
const LEAD = "g";
export const LEAD_MS = 1_000;
/** How long after G the hint of the second keys shows, so a quick G H does not flash it. */
export const HINT_MS = 400;

export type GoKey = "h" | "i" | "p" | "r" | "c" | "w";

/** Where each second key goes: a hub page, or a page of the project shown (or the visitor's only project). */
export const GO: Record<GoKey, { id: ShortcutId; label: NavLabel } & ({ href: Route } | { segment: string })> = {
  h: { id: "goHome", label: "home", href: "/" },
  i: { id: "goInbox", label: "inbox", href: "/inbox" },
  p: { id: "goPlans", label: "plans", segment: "plans" },
  r: { id: "goRuns", label: "runs", segment: "runs" },
  c: { id: "goCurator", label: "curator", segment: "curator" },
  w: { id: "goWorkers", label: "workers", href: "/workers" },
};

/** The sidebar's items that a "go to" sequence opens, for the `kbd` beside each. */
export const NAV_SHORTCUTS: Partial<Record<NavLabel, ShortcutId>> = Object.fromEntries(
  Object.values(GO).map((target) => [target.label, target.id]),
);

function isGoKey(letter: string): letter is GoKey {
  return letter in GO;
}

/** The letter on the key: the character typed, or on a layout without Latin letters the key's place (KeyG). */
function letterOf(event: Pick<KeyboardEvent, "key" | "code">): string | null {
  if (/^[a-z]$/i.test(event.key)) return event.key.toLowerCase();
  if (event.key.length === 1 && /^Key[A-Z]$/.test(event.code ?? "")) return event.code.slice(3).toLowerCase();
  return null;
}

export type KeyAction = { kind: "lead" } | { kind: "go"; key: GoKey } | { kind: "dispatch" } | { kind: "help" } | null;

type KeyEvent = Pick<
  KeyboardEvent,
  "key" | "code" | "shiftKey" | "defaultPrevented" | "isComposing" | "repeat" | "ctrlKey" | "metaKey" | "altKey" | "target"
>;

/**
 * What a key press asks of the shell, `afterLead` being whether G was pressed less than a second ago: nothing when
 * the key belongs to what has focus (`characterKeyBlocked`), `?` with or without Shift (layouts differ), and the
 * letters without Shift.
 */
export function readKey(event: KeyEvent, afterLead: boolean): KeyAction {
  if (characterKeyBlocked(event)) return null;
  if (event.key === "?") return { kind: "help" };
  if (event.shiftKey) return null;
  const letter = letterOf(event);
  if (letter === null) return null;
  if (afterLead && isGoKey(letter)) return { kind: "go", key: letter };
  if (letter === LEAD) return { kind: "lead" };
  if (letter === "d") return { kind: "dispatch" };
  return null;
}
