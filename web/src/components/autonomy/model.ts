import { AUTONOMY_LEVELS, levelRank, type Mode, modeRank } from "@/components/data/autonomy";

import type { AutonomySettings, Level, PrefSet } from "./queries";

/**
 * How the settings page reads the hub's answer and builds the next preference: the draft the form edits, which
 * options lie above the ceiling (shown dimmed with its source), and the body of PUT /v1/autonomy/prefs. Pure
 * functions, shared by the page and its tests.
 */

export const LEVELS: readonly Level[] = AUTONOMY_LEVELS;
export const CLASSES = ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10", "K11", "K12"] as const;

/** The mode of each class at each level, as `autonomy_levels.PRESETS` sets it (without the conditions). */
const PRESET_ROWS: Record<Level, readonly Mode[]> = {
  A0: ["auto", "deny", "deny", "deny", "deny", "deny", "deny", "deny", "deny", "deny", "deny", "deny"],
  A1: ["auto", "auto", "auto", "auto", "ask", "deny", "auto", "deny", "deny", "deny", "deny", "deny"],
  A2: ["auto", "auto", "auto", "auto", "notify", "ask", "auto", "deny", "deny", "ask", "deny", "deny"],
  A3: ["auto", "auto", "auto", "auto", "auto", "notify", "auto", "ask", "ask", "ask", "ask", "deny"],
  A4: ["auto", "auto", "auto", "auto", "auto", "auto", "auto", "notify", "notify", "notify", "notify", "ask"],
  A5: ["auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto"],
};

export function presetMode(level: Level, klass: string): Mode {
  const at = CLASSES.indexOf(klass as (typeof CLASSES)[number]);
  return PRESET_ROWS[level][at] ?? "auto";
}

/** The form's state: the level and each class's override ("" for the level's own mode). */
export type Draft = { level: Level; classes: Record<string, string> };

export function draftOf(settings: AutonomySettings): Draft {
  return { level: settings.pref_level ?? "A5", classes: { ...settings.pref_classes } };
}

function overrides(draft: Draft): Record<string, string> {
  return Object.fromEntries(Object.entries(draft.classes).filter(([, mode]) => mode !== ""));
}

/** Whether the draft differs from the preference the hub holds. */
export function isChanged(settings: AutonomySettings, draft: Draft): boolean {
  const held = settings.pref_classes;
  const wanted = overrides(draft);
  if (draft.level !== (settings.pref_level ?? "A5")) return true;
  const keys = new Set([...Object.keys(held), ...Object.keys(wanted)]);
  return [...keys].some((klass) => held[klass] !== wanted[klass]);
}

/**
 * The body that makes the draft the preference: the level and every override. A class whose override the draft drops
 * needs a reset, since the hub merges the overrides onto those before; the reset drops them all, and the body names
 * every one it keeps.
 */
export function prefBody(settings: AutonomySettings, draft: Draft): PrefSet {
  const wanted = overrides(draft);
  const dropped = Object.keys(settings.pref_classes).some((klass) => !(klass in wanted));
  return { level: draft.level, classes: wanted, reset: dropped };
}

/** Whether `level` lies above the ceiling the sources other than the preference set. */
export function levelAboveCeiling(settings: AutonomySettings, level: Level): boolean {
  return levelRank(level) > levelRank(settings.ceiling);
}

/** Whether `mode` lies above the loosest mode the ceiling allows the class `klass`. */
export function modeAboveCeiling(settings: AutonomySettings, klass: string, mode: string): boolean {
  const held = settings.classes[klass];
  return held ? modeRank(mode.split(":")[0] ?? mode) > modeRank(held.ceiling) : false;
}

/** The classes the ceiling lowers below what the preference sets: the page flags them. */
export function loweredClasses(settings: AutonomySettings): string[] {
  return CLASSES.filter((klass) => settings.classes[klass]?.lowered);
}

/** The figures of a class worth a line: null when the hub records none, or every count is zero. */
export function figureCounts(settings: AutonomySettings, klass: string): Record<string, number> | null {
  const figures = settings.classes[klass]?.figures;
  if (!figures) return null;
  const found = Object.entries(figures).filter((entry): entry is [string, number] => typeof entry[1] === "number" && entry[1] > 0);
  return found.length > 0 ? Object.fromEntries(found) : {};
}
