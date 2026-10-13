import type { AccountUsage } from "./queries";

/**
 * An account's usage limits as the Accounts page draws them (docs/credentials.md, section "Accounts"): one window per
 * limit the hub kept, with the share of it used, when it resets and the tone of its bar. The CLI gives a share as a
 * fraction of the limit (0.35); one above 1 is read as the limit reached, 100%, never as a percent. A window whose
 * reset has passed since the report started again from nothing: it reads 0%, said to be reset.
 */

/** A share at or above this many percent draws in the warning tone. */
export const WARN_PERCENT = 80;

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;
/** The name of the limit a report gave without naming it (no rate_limit_type and no window). */
export const UNNAMED = "";

export type UsageTone = "normal" | "warning" | "danger";

export type UsageWindowView = {
  name: string;
  /** Whole percent used, 0 to 100; null when the CLI gave no share for the window. */
  percent: number | null;
  resetsAt: string | null;
  /** The window's reset has passed since the report, so its usage started again from 0. */
  reset: boolean;
  /** The limit the CLI said the account hit (status rejected), not reset since. */
  limited: boolean;
  tone: UsageTone;
};

/** A share the CLI gave as a fraction of the limit, in whole percent from 0 to 100; null for none. */
export function sharePercent(share: number | null | undefined): number | null {
  if (typeof share !== "number" || !Number.isFinite(share)) return null;
  return Math.round(Math.min(Math.max(share, 0), 1) * 100);
}

/** The session windows first, then the weekly ones, each family's own window before its models', then the rest. */
function rank(name: string): number {
  if (name === "five_hour") return 0;
  if (name.startsWith("five_hour_")) return 1;
  if (name === "seven_day") return 2;
  if (name.startsWith("seven_day_")) return 3;
  return 4;
}

const passed = (at: string | null | undefined, now: number) => {
  const time = at ? Date.parse(at) : Number.NaN;
  return Number.isFinite(time) && time <= now;
};

/**
 * The windows of an account's last usage at `now`, in the order the page lists them: its `windows`, and the limit the
 * report names (`rate_limit_type`) from the report's own share and reset when no window holds them. A window with
 * neither a share nor a reset says nothing and is left out.
 */
export function usageWindows(usage: AccountUsage | null | undefined, now: number): UsageWindowView[] {
  if (!usage) return [];
  const windows = { ...usage.windows };
  const named = usage.rate_limit_type ?? (Object.keys(windows).length === 0 ? UNNAMED : null);
  if (named !== null) {
    const own = windows[named] ?? {};
    windows[named] = { utilization: own.utilization ?? usage.utilization, resets_at: own.resets_at ?? usage.resets_at };
  }
  return Object.entries(windows)
    .map(([name, window]): UsageWindowView => {
      const resetsAt = window.resets_at ?? null;
      const reset = passed(resetsAt, now);
      const limited = !reset && usage.status === "rejected" && name === named;
      const percent = reset ? 0 : (sharePercent(window.utilization) ?? (limited ? 100 : null));
      const tone = limited || (percent ?? 0) >= 100 ? "danger" : (percent ?? 0) >= WARN_PERCENT ? "warning" : "normal";
      return { name, percent, resetsAt, reset, limited, tone };
    })
    .filter((window) => window.percent !== null || window.resetsAt !== null)
    .sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
}

/** What the CLI said of the account at `now`: its status, or allowed once the limit it reported has reset. */
export function usageStatus(usage: AccountUsage, now: number): AccountUsage["status"] {
  const resetsAt = usage.resets_at ?? (usage.rate_limit_type ? usage.windows?.[usage.rate_limit_type]?.resets_at : null);
  return usage.status !== "allowed" && passed(resetsAt, now) ? "allowed" : usage.status;
}

export type WindowLabel =
  | { key: "five_hour" | "seven_day" | "unnamed" }
  | { key: "session" | "weekly"; model: string }
  | { key: "other"; text: string };

const words = (name: string) => name.split(/[_.-]+/).filter(Boolean);
const capital = (word: string) => word.charAt(0).toUpperCase() + word.slice(1);

/**
 * How a window's name reads: five_hour is the session and seven_day the week of all models; a model after either
 * (seven_day_opus) is that window of the model ("Opus"); any other name is its words, the first capitalised.
 */
export function windowLabel(name: string): WindowLabel {
  if (name === UNNAMED) return { key: "unnamed" };
  if (name === "five_hour" || name === "seven_day") return { key: name };
  for (const [prefix, key] of [["five_hour_", "session"], ["seven_day_", "weekly"]] as const) {
    const model = words(name.slice(prefix.length));
    if (name.startsWith(prefix) && model.length > 0) return { key, model: model.map(capital).join(" ") };
  }
  const text = words(name).join(" ");
  return { key: "other", text: capital(text || name) };
}

export type ResetWhen =
  | { kind: "in"; hours: number; minutes: number }
  | { kind: "weekday" | "date" | "past"; at: Date };

/**
 * How to say when a window resets, from `now`: in hours and minutes within a day (rounded up to the minute, so it never
 * says 0 before the reset), by weekday and time within a week, by date and time past that, and past once it is.
 */
export function resetWhen(resetsAt: string, now: number): ResetWhen {
  const at = new Date(resetsAt);
  const left = at.getTime() - now;
  if (left <= 0) return { kind: "past", at };
  if (left <= DAY) {
    const minutes = Math.ceil(left / MINUTE);
    return { kind: "in", hours: Math.floor(minutes / 60), minutes: minutes % 60 };
  }
  return { kind: left < 7 * DAY ? "weekday" : "date", at };
}
