import type { NotificationSettings, NotificationSettingsIn, Tier, TierKind, TierSetting } from "@/lib/notification-queries";

/**
 * The pure parts of the notification settings page (docs/notifications.md): which kinds of notification come first,
 * what a tier's select holds, and the schedule (quiet hours, time zone, time of the summary push) as a draft the form
 * edits and the body PUT /v1/me/notification-settings takes.
 */

/** `tables_push.TIERS`: pushed at once; only in Today and the Inbox; in the Inbox alone. */
export const TIERS = ["interrupt", "brief", "off"] as const satisfies readonly Tier[];

/** The select's value of a kind the member never chose: the hub's default holds. */
export const DEFAULT_CHOICE = "default";
export type TierChoice = Tier | typeof DEFAULT_CHOICE;

/**
 * The kinds the page shows first: those `push_tiers.DEFAULT_INTERRUPTS` pushes for some of their notifications, then
 * the two a member decides most about. Every other kind, those of a later hub included, folds under them.
 */
export const FIRST_KINDS = ["decision", "run_failed", "release", "merge_paused", "proposal", "today"] as const satisfies readonly TierKind[];
/** `push_tiers.DEFAULT_INTERRUPTS`: their default is interrupt only for some of their notifications, which a hint says. */
export const DEFAULT_INTERRUPTS = ["decision", "run_failed", "release", "merge_paused"] as const satisfies readonly TierKind[];

export function isDefaultInterrupt(kind: string): boolean {
  return (DEFAULT_INTERRUPTS as readonly string[]).includes(kind);
}

/** The tiers as the page lists them: the first kinds in their order, then the rest in the hub's. */
export function tierGroups(tiers: readonly TierSetting[]): { first: TierSetting[]; rest: TierSetting[] } {
  const first = FIRST_KINDS.map((kind) => tiers.find((setting) => setting.kind === kind)).filter((setting): setting is TierSetting => setting !== undefined);
  const rest = tiers.filter((setting) => !(FIRST_KINDS as readonly string[]).includes(setting.kind));
  return { first, rest };
}

/** The tier the hub gives a kind until the member chooses one, as the settings show it (`push_tiers.kind_default`). */
export function defaultTier(kind: string): Tier {
  return isDefaultInterrupt(kind) ? "interrupt" : "brief";
}

export function choiceOf(setting: TierSetting): TierChoice {
  return setting.chosen ? setting.tier : DEFAULT_CHOICE;
}

/** The body that sets one kind's tier, or puts it back to the hub's default. */
export function tierBody(kind: TierKind, choice: TierChoice): NotificationSettingsIn {
  return { tiers: [{ kind, tier: choice === DEFAULT_CHOICE ? null : choice }] };
}

// The schedule

/** `curator.TIME_OF_DAY`: HH:MM on 24 hours. */
export const TIME_OF_DAY = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
/** `curator.TIME_ZONE` and `curator.MAX_TIME_ZONE_CHARS`; the hub checks the name against Postgres's list as well. */
export const TIME_ZONE = /^[A-Za-z][A-Za-z0-9_+-]*(\/[A-Za-z0-9_+-]+)*$/;
export const MAX_TIME_ZONE_CHARS = 64;
/** Quiet hours the form offers when the member turns them on for the first time. */
export const QUIET_START = "22:00";
export const QUIET_END = "07:00";

export type ScheduleDraft = { quiet: boolean; start: string; end: string; timeZone: string; digestAt: string };
export type ScheduleField = "start" | "end" | "timeZone" | "digestAt";
export type ScheduleProblem = "time" | "sameTime" | "timeZone";
export type ScheduleErrors = Partial<Record<ScheduleField, ScheduleProblem>>;

/** A zone as this browser resolves it, so an alias and the name it stands for read alike; the name itself if unknown. */
function resolvedZone(zone: string): string {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: zone }).resolvedOptions().timeZone;
  } catch {
    return zone;
  }
}

/** Whether two IANA names are one zone: Chromium names Asia/Ho_Chi_Minh Asia/Saigon, its older name. */
export function sameZone(a: string, b: string): boolean {
  return a === b || resolvedZone(a) === resolvedZone(b);
}

/**
 * The schedule as the form starts it: what the member chose, and for a time zone they never chose the browser's own
 * (`ownZone`, null on the server), which saving then keeps; the hub's until the browser says, or when the browser's is
 * the hub's under another name.
 */
export function scheduleDraft(settings: NotificationSettings, ownZone: string | null): ScheduleDraft {
  const own = settings.time_zone_chosen || ownZone === null || sameZone(ownZone, settings.time_zone) ? null : ownZone;
  return {
    quiet: settings.quiet_hours !== null,
    start: settings.quiet_hours?.start ?? QUIET_START,
    end: settings.quiet_hours?.end ?? QUIET_END,
    timeZone: own ?? settings.time_zone,
    digestAt: settings.digest_at,
  };
}

/** What is wrong with each field of the draft, as the hub would refuse it; empty when nothing is. */
export function scheduleErrors(draft: ScheduleDraft): ScheduleErrors {
  const errors: ScheduleErrors = {};
  if (draft.quiet) {
    if (!TIME_OF_DAY.test(draft.start)) errors.start = "time";
    if (!TIME_OF_DAY.test(draft.end)) errors.end = "time";
    else if (draft.start === draft.end) errors.end = "sameTime";
  }
  const zone = draft.timeZone.trim();
  if (!zone || zone.length > MAX_TIME_ZONE_CHARS || !TIME_ZONE.test(zone)) errors.timeZone = "timeZone";
  if (!TIME_OF_DAY.test(draft.digestAt)) errors.digestAt = "time";
  return errors;
}

/**
 * Whether saving the draft changes what the hub holds. A time zone the member never chose is the hub's: the browser's,
 * once it differs, counts as a change, so the form offers to save it.
 */
export function scheduleChanged(settings: NotificationSettings, draft: ScheduleDraft): boolean {
  const quiet = settings.quiet_hours;
  if (draft.quiet !== (quiet !== null)) return true;
  if (draft.quiet && quiet !== null && (draft.start !== quiet.start || draft.end !== quiet.end)) return true;
  if (!sameZone(draft.timeZone.trim(), settings.time_zone)) return true;
  return draft.digestAt !== settings.digest_at;
}

/** The body that saves the draft: quiet hours or none, the time zone and the time of the summary push. */
export function scheduleBody(draft: ScheduleDraft): NotificationSettingsIn {
  return {
    quiet_hours: draft.quiet ? { start: draft.start, end: draft.end } : null,
    time_zone: draft.timeZone.trim(),
    digest_at: draft.digestAt,
  };
}

/** Whether the quiet hours run past midnight: the end is not after the start. */
export function overnight(start: string, end: string): boolean {
  return end <= start;
}
