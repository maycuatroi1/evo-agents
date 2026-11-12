import type { CronJob, CronJobPut, CronKind } from "./queries";

/**
 * A cron expression in words, for the common shapes: every N minutes, hourly, every N hours, daily, on weekdays, on
 * some days of the week, monthly on a day. Anything else (month lists, steps over days, ranges of hours) is null, and
 * the page shows the expression alone. Days of the week are 0 to 6 from Sunday (7 reads as Sunday, as in cron); times
 * are "HH:MM" in the job's own time zone.
 */
export type ScheduleWords =
  | { key: "everyMinute" }
  | { key: "everyMinutes"; n: number }
  | { key: "hourly"; minute: number }
  | { key: "everyHours"; n: number; minute: number }
  | { key: "daily"; times: string[] }
  | { key: "weekdays"; times: string[] }
  | { key: "weekly"; days: number[]; times: string[] }
  | { key: "monthly"; day: number; times: string[] };

const NUMBER = /^\d+$/;
const STEP = /^\*\/(\d+)$/;
const MAX_TIMES = 6;

function numbers(field: string, max: number): number[] | null {
  const parts = field.split(",");
  if (!parts.every((part) => NUMBER.test(part))) return null;
  const values = parts.map(Number);
  return values.every((value) => value <= max) ? [...new Set(values)].sort((a, b) => a - b) : null;
}

/** The days a day-of-week field names (lists and ranges), Monday first and Sunday last; null for steps or names. */
function weekDays(field: string): number[] | null {
  const days = new Set<number>();
  for (const part of field.split(",")) {
    const range = /^(\d)-(\d)$/.exec(part);
    if (range) {
      const [from, to] = [Number(range[1]), Number(range[2])];
      if (from > to || to > 7) return null;
      for (let day = from; day <= to; day += 1) days.add(day % 7);
    } else if (/^\d$/.test(part) && Number(part) <= 7) {
      days.add(Number(part) % 7);
    } else {
      return null;
    }
  }
  return [...days].sort((a, b) => ((a + 6) % 7) - ((b + 6) % 7));
}

const pad = (value: number) => String(value).padStart(2, "0");

export function describeCron(expression: string): ScheduleWords | null {
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [minute, hour, dom, month, dow] = fields;
  if (month !== "*") return null;
  const anyDay = dom === "*" && dow === "*";
  if (fields.every((field) => field === "*")) return { key: "everyMinute" };
  const minuteStep = STEP.exec(minute);
  if (minuteStep && hour === "*" && anyDay) return { key: "everyMinutes", n: Number(minuteStep[1]) };
  if (!NUMBER.test(minute) && !minute.includes(",")) return null;
  const minutes = numbers(minute, 59);
  if (!minutes) return null;
  if (minutes.length === 1 && hour === "*" && anyDay) return { key: "hourly", minute: minutes[0] };
  const hourStep = STEP.exec(hour);
  if (minutes.length === 1 && hourStep && anyDay) return { key: "everyHours", n: Number(hourStep[1]), minute: minutes[0] };
  const hours = numbers(hour, 23);
  if (!hours || hours.length * minutes.length > MAX_TIMES) return null;
  const times = hours.flatMap((h) => minutes.map((m) => `${pad(h)}:${pad(m)}`));
  if (anyDay) return { key: "daily", times };
  if (dom === "*") {
    const days = weekDays(dow);
    if (!days) return null;
    if (days.length === 7) return { key: "daily", times };
    if (days.join(",") === "1,2,3,4,5") return { key: "weekdays", times };
    return { key: "weekly", days, times };
  }
  if (dow === "*" && NUMBER.test(dom) && Number(dom) >= 1 && Number(dom) <= 31) return { key: "monthly", day: Number(dom), times };
  return null;
}

/** The browser's IANA time zone, which a new job starts with; UTC when the browser does not say. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Every IANA zone the browser knows, for the time zone field's suggestions; empty where it cannot list them. */
export function knownTimeZones(): string[] {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: "timeZone") => string[] };
  return typeof intl.supportedValuesOf === "function" ? intl.supportedValuesOf("timeZone") : [];
}

// The form

/** `NAME_PATTERN` of the API: lower-case letters, digits and hyphens, a letter or digit first, at most 63. */
export const NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}$/;
export const DEFAULT_TIMEOUT = 30;
export const MAX_TIMEOUT = 240;
export const MAX_PROMPT = 20_000;
export const MAX_COMMAND = 4_000;
export const NEW_CRON = "0 9 * * 1-5";

/** The fields of the dialog, as text; `chat` and `repo` are "" for the owner's direct chat and for no repo. */
export type CronForm = {
  name: string;
  kind: CronKind;
  cron: string;
  timezone: string;
  agent: string;
  chat: string;
  prompt: string;
  plan: string;
  command: string;
  repo: string;
  timeout: string;
};

export type CronField = "name" | "cron" | "timezone" | "agent" | "prompt" | "plan" | "command" | "timeout";

export function emptyForm(timezone: string): CronForm {
  return { name: "", kind: "agent", cron: NEW_CRON, timezone, agent: "", chat: "", prompt: "", plan: "", command: "", repo: "", timeout: String(DEFAULT_TIMEOUT) };
}

export function formOf(job: CronJob): CronForm {
  return {
    name: job.name,
    kind: job.kind,
    cron: job.cron,
    timezone: job.timezone,
    agent: job.agent ?? "",
    chat: job.chat_id === null ? "" : String(job.chat_id),
    prompt: job.prompt ?? "",
    plan: job.plan ?? "",
    command: job.command ?? "",
    repo: job.repo ?? "",
    timeout: String(job.timeout_minutes ?? DEFAULT_TIMEOUT),
  };
}

/** The fields the dialog marks before it sends anything; the hub checks the rest (the cron's gap, the zone, names). */
export function formProblems(form: CronForm, isNew: boolean): CronField[] {
  const wrong: CronField[] = [];
  if (isNew && !NAME_PATTERN.test(form.name.trim())) wrong.push("name");
  if (form.cron.trim().split(/\s+/).length !== 5) wrong.push("cron");
  if (!form.timezone.trim()) wrong.push("timezone");
  if (form.kind === "agent") {
    if (!form.agent.trim()) wrong.push("agent");
    if (!form.prompt.trim() || form.prompt.length > MAX_PROMPT) wrong.push("prompt");
  } else if (form.kind === "plan") {
    if (!form.plan.trim()) wrong.push("plan");
  } else {
    if (!form.command.trim() || form.command.length > MAX_COMMAND) wrong.push("command");
    const timeout = Number(form.timeout);
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > MAX_TIMEOUT) wrong.push("timeout");
  }
  return wrong;
}

/** What a write sends: the fields of its kind alone, since the hub refuses those of another; `revision` 0 creates. */
export function bodyOf(form: CronForm, revision: number): CronJobPut {
  const base = { cron: form.cron.trim().split(/\s+/).join(" "), timezone: form.timezone.trim(), kind: form.kind, if_revision: revision };
  if (form.kind === "agent") {
    return { ...base, agent: form.agent.trim().replace(/^@/, ""), chat_id: form.chat ? Number(form.chat) : null, prompt: form.prompt.trim() };
  }
  if (form.kind === "plan") return { ...base, plan: form.plan.trim() };
  return { ...base, command: form.command.trim(), repo: form.repo || null, timeout_minutes: Number(form.timeout) };
}
