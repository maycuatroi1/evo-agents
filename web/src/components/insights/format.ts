import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { durationParts, utcMidnight } from "./model";

export type InsightFormat = {
  /** "Tue, Oct 7": a row of a table, the head of a tooltip. */
  day: (day: string) => string;
  /** "Oct 7": a tick of the day axis, the ends of the range. */
  shortDay: (day: string) => string;
  count: (value: number) => string;
  /** "1.2M": a tick of the token axis. */
  compact: (value: number) => string;
  /** "33%" from 0.333. */
  percent: (rate: number) => string;
  /** "under 1s", "45s", "8m 12s", "1h 5m", as the kit writes a duration. */
  duration: (seconds: number) => string;
  /** "30s", "5m", "1h": a tick of the duration axis, always a round step. */
  durationTick: (seconds: number) => string;
};

/**
 * How the Insights page writes its figures, in the visitor's language. Days are UTC days, so they are formatted in
 * UTC whatever the visitor's time zone, and never move to the day before.
 */
export function useInsightFormat(): InsightFormat {
  const t = useTranslations("insights.format");
  const format = useFormatter();
  return useMemo(
    () => ({
      day: (day) => format.dateTime(utcMidnight(day), { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }),
      shortDay: (day) => format.dateTime(utcMidnight(day), { month: "short", day: "numeric", timeZone: "UTC" }),
      count: (value) => format.number(value),
      compact: (value) => format.number(value, { notation: "compact", maximumFractionDigits: 1 }),
      percent: (rate) => format.number(rate, { style: "percent", maximumFractionDigits: 0 }),
      duration: (seconds) => {
        if (seconds < 1) return t("under");
        const { hours, minutes, seconds: rest } = durationParts(seconds);
        if (hours > 0) return t("hours", { hours, minutes });
        if (minutes > 0) return t("minutes", { minutes, seconds: rest });
        return t("seconds", { seconds: rest });
      },
      durationTick: (seconds) => {
        if (seconds <= 0) return format.number(0);
        if (seconds % 3600 === 0) return t("tickHours", { hours: seconds / 3600 });
        if (seconds % 60 === 0) return t("tickMinutes", { minutes: seconds / 60 });
        return t("tickSeconds", { seconds });
      },
    }),
    [t, format],
  );
}
