"use client";

import { Bot, CircleCheck, CirclePause, CircleX, Clock, Hourglass, ListChecks, type LucideIcon, Terminal, TimerOff } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill } from "@/components/status/status-badge";
import { useNow } from "@/hooks/use-now";

import { describeCron, type ScheduleWords } from "./model";
import { type CronFire, type CronJob, type CronKind, cronRunHref, type FireOutcome } from "./queries";

/**
 * The states of the Cron jobs page: a fire's outcome and a paused job, as the shared `StatusPill`. Only this page shows
 * them, so their looks live here; each has its words under `cron` in messages/en.json and vi.json.
 */
export const FIRE_LOOKS: Record<FireOutcome | "pending", StatusLook> = {
  started: { tone: "success", icon: CircleCheck },
  busy: { tone: "attention", icon: Hourglass },
  missed: { tone: "neutral", icon: TimerOff },
  refused: { tone: "danger", icon: CircleX },
  pending: { tone: "neutral", icon: Clock },
};
const PAUSED_LOOK: StatusLook = { tone: "attention", icon: CirclePause };
export const KIND_ICONS: Record<CronKind, LucideIcon> = { agent: Bot, plan: ListChecks, command: Terminal };

export function FireOutcomeBadge({ fire }: { fire: CronFire }) {
  const t = useTranslations("cron.outcome");
  const outcome = fire.outcome ?? "pending";
  return <StatusPill look={FIRE_LOOKS[outcome]} label={t(outcome)} data-outcome={outcome} data-testid="cron-fire-outcome" />;
}

export function PausedBadge() {
  const t = useTranslations("cron.list");
  return <StatusPill look={PAUSED_LOOK} label={t("paused")} data-testid="cron-paused" />;
}

/** What a job is, as a tag: Agent, Plan or Command. */
export function KindTag({ kind }: { kind: CronKind }) {
  const t = useTranslations("cron.kind");
  const Icon = KIND_ICONS[kind];
  return (
    <Tag data-kind={kind}>
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

/** What a job starts, in one line: the agent it messages, the plan it runs, or its command line. */
export function workText(job: CronJob): string {
  if (job.kind === "agent") return `@${job.agent ?? "?"}`;
  if (job.kind === "plan") return job.plan ?? "?";
  return job.command ?? "";
}

/** "in 5 hours" or "3 minutes ago", ticking; the full time is its tooltip, and the first render shows it whole. */
export function Relative({ value }: { value: string }) {
  const format = useFormatter();
  const now = useNow(true);
  const date = new Date(value);
  const full = format.dateTime(date, { dateStyle: "medium", timeStyle: "short" });
  return (
    <time dateTime={value} title={full} className="whitespace-nowrap tabular-nums">
      {now === null ? full : format.relativeTime(date, now)}
    </time>
  );
}

/** A cron expression in the visitor's words ("Weekdays at 09:00"), or "Custom schedule" for a shape it does not read. */
export function useScheduleText(): (expression: string) => string {
  const t = useTranslations("cron.schedule");
  const format = useFormatter();
  const list = (items: string[]) => format.list(items, { type: "conjunction" });
  const day = (weekday: number) => format.dateTime(new Date(Date.UTC(2024, 0, 7 + weekday, 12)), { weekday: "long", timeZone: "UTC" });
  const words = (shape: ScheduleWords): string => {
    switch (shape.key) {
      case "everyMinute":
        return t("everyMinute");
      case "everyMinutes":
        return t("everyMinutes", { n: shape.n });
      case "hourly":
        return t("hourly", { minute: shape.minute });
      case "everyHours":
        return t("everyHours", { n: shape.n, minute: shape.minute });
      case "daily":
        return t("daily", { times: list(shape.times) });
      case "weekdays":
        return t("weekdays", { times: list(shape.times) });
      case "weekly":
        return t("weekly", { days: list(shape.days.map(day)), times: list(shape.times) });
      case "monthly":
        return t("monthly", { day: shape.day, times: list(shape.times) });
    }
  };
  return (expression) => {
    const shape = describeCron(expression);
    return shape ? words(shape) : t("custom");
  };
}

/** The schedule in words over its expression and zone, the way the list and the history head show it. */
export function ScheduleLine({ job }: { job: CronJob }) {
  const text = useScheduleText();
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span className="truncate text-foreground" data-testid="cron-schedule-text">
        {text(job.cron)}
      </span>
      <span className="flex min-w-0 gap-2 text-xs text-fg-subtle" title={`${job.cron} (${job.timezone})`}>
        <span className="shrink-0 font-mono" data-testid="cron-expression">
          {job.cron}
        </span>
        <span className="truncate">{job.timezone}</span>
      </span>
    </span>
  );
}

/** A fire's run, as `#N` to its page; nothing for a fire that started none. */
export function RunLink({ project, fire }: { project: string; fire: CronFire }) {
  const t = useTranslations("cron.list");
  if (fire.run_id === null) return null;
  return (
    <Link href={cronRunHref(project, fire.run_id)} className="font-mono text-xs text-brand hover:underline" aria-label={t("runLink", { id: fire.run_id })} data-testid="cron-run-link">
      #{fire.run_id}
    </Link>
  );
}

/**
 * The last fire in the list: its run and when, with a pill only when it did not start one (busy, missed, refused, or
 * still waiting for the hub), its reason under it in `danger` for a refusal.
 */
export function LastFire({ project, job }: { project: string; job: CronJob }) {
  const t = useTranslations("cron.list");
  const fire = job.last_fire;
  if (!fire) return <span className="text-[13px] text-fg-subtle">{t("neverFired")}</span>;
  return (
    <span className="flex min-w-0 flex-col gap-0.5" data-testid="cron-last-fire">
      <span className="flex items-center gap-2">
        {fire.outcome === "started" ? null : <FireOutcomeBadge fire={fire} />}
        <RunLink project={project} fire={fire} />
        <span className="text-xs text-fg-subtle">
          <Relative value={fire.scheduled_for} />
        </span>
      </span>
      {fire.reason ? (
        <span className={`max-w-[32ch] truncate text-xs ${fire.outcome === "refused" ? "text-danger" : "text-fg-subtle"}`} title={fire.reason}>
          {fire.reason}
        </span>
      ) : null}
    </span>
  );
}
