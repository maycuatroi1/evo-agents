"use client";

import { Gauge } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { Meter, METER_TEXT } from "@/components/data/meter";
import { Badge } from "@/components/ui/badge";
import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import type { AccountFigures } from "./queries";
import { resetWhen, usageStatus, type UsageWindowView, usageWindows, windowLabel } from "./usage-model";

type Format = ReturnType<typeof useFormatter>;
type T = ReturnType<typeof useTranslations<"accounts.usage">>;

const WEEKDAY = { weekday: "short", hour: "numeric", minute: "2-digit" } as const;
const DATE = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" } as const;
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function useWindowName(name: string): string {
  const t = useTranslations("accounts.usage.windows");
  const label = windowLabel(name);
  if (label.key === "other") return label.text;
  if (label.key === "session" || label.key === "weekly") return t(label.key, { model: label.model });
  return t(label.key);
}

/**
 * When a window resets: "Resets in 2 hr 51 min" within a day, "Resets Tue 7:00 AM" within a week, the date past
 * that, "Reset Tue 7:00 AM" once passed. Before the page has the browser's clock (on the server and while it
 * hydrates) it says the time alone, so both renders agree.
 */
function resetText(t: T, format: Format, resetsAt: string, now: number | null): string {
  const when = now === null ? null : resetWhen(resetsAt, now);
  const at = new Date(resetsAt);
  if (when === null) return t("resetsAt", { time: format.dateTime(at, DATE) });
  if (when.kind === "in") {
    const { hours, minutes } = when;
    const duration = hours && minutes ? t("duration.hoursMinutes", { hours, minutes }) : hours ? t("duration.hours", { hours }) : t("duration.minutes", { minutes });
    return t("resetsIn", { duration });
  }
  if (when.kind === "past") {
    const recent = (now ?? 0) - at.getTime() < WEEK_MS;
    return t("wasReset", { time: format.dateTime(at, recent ? WEEKDAY : DATE) });
  }
  return t("resetsAt", { time: format.dateTime(at, when.kind === "weekday" ? WEEKDAY : DATE) });
}

/** One limit: its name and the share used on a line, the bar, then when it resets. */
function UsageWindow({ window, now }: { window: UsageWindowView; now: number | null }) {
  const t = useTranslations("accounts.usage");
  const format = useFormatter();
  const name = useWindowName(window.name);
  const id = useId();
  const figure = window.limited ? t("limited") : window.percent === null ? t("notReported") : t("percent", { percent: window.percent });
  return (
    <li className="flex min-w-0 flex-col gap-1.5" data-testid="account-usage-window" data-window={window.name} data-tone={window.tone}>
      <div className="flex min-w-0 items-baseline justify-between gap-3 text-[13px] leading-[18px]">
        <span id={id} className="min-w-0 truncate font-medium text-foreground" title={name}>
          {name}
        </span>
        <span className={cn("shrink-0 tabular-nums", window.percent === null ? "text-fg-subtle" : cn("font-medium", METER_TEXT[window.tone]))}>
          {figure}
        </span>
      </div>
      {window.percent === null ? null : <Meter value={window.percent} tone={window.tone} labelledBy={id} valueText={figure} />}
      {window.resetsAt ? (
        <span className="text-xs text-fg-subtle" title={format.dateTime(new Date(window.resetsAt), { dateStyle: "full", timeStyle: "short" })}>
          {resetText(t, format, window.resetsAt, now)}
        </span>
      ) : null}
    </li>
  );
}

/**
 * The usage limits of an account, under its row of the Accounts page: when a run last reported them, then a bar per
 * window the hub kept (usage-model.ts says how they read), side by side as the width allows and stacked on a phone. A
 * status the bars do not carry (near its limit, a limit without a window) is a badge beside the time. Before any
 * report, one quiet line says so.
 */
export function UsageLimits({ account, pageNow }: { account: Pick<AccountFigures, "name" | "last_usage" | "last_usage_at">; pageNow: number }) {
  const t = useTranslations("accounts.usage");
  const format = useFormatter();
  const usage = account.last_usage;
  const clock = useNow(usage !== null);
  if (!usage) {
    return (
      <p className="text-xs text-fg-subtle" data-testid="account-usage" data-status="none">
        {t("none")}
      </p>
    );
  }
  const now = clock ?? pageNow;
  const windows = usageWindows(usage, now);
  const status = usageStatus(usage, now);
  const reported = account.last_usage_at;
  const badge = status !== "allowed" && !windows.some((window) => window.limited);
  return (
    <div role="group" aria-label={t("label", { name: account.name })} className="flex flex-col gap-2.5" data-testid="account-usage" data-status={status}>
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-subtle">
        <Gauge className="size-3.5 shrink-0" aria-hidden="true" />
        <span>
          {reported
            ? t.rich("titleReported", {
                time: () => (
                  <time dateTime={reported} title={format.dateTime(new Date(reported), { dateStyle: "medium", timeStyle: "short" })} data-testid="account-usage-reported">
                    {clock === null
                      ? format.dateTime(new Date(reported), { dateStyle: "short", timeStyle: "short" })
                      : format.relativeTime(new Date(reported), Math.max(clock, Date.parse(reported)))}
                  </time>
                ),
              })
            : t("title")}
        </span>
        {badge ? (
          <Badge variant={status === "rejected" ? "destructive" : "warning"}>
            <Gauge aria-hidden="true" />
            {t(`status.${status}`)}
          </Badge>
        ) : null}
      </p>
      {windows.length > 0 ? (
        <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(14rem,100%),1fr))] gap-x-8 gap-y-3">
          {windows.map((window) => (
            <UsageWindow key={window.name} window={window} now={clock} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}
