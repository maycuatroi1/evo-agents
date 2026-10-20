"use client";

import { useFormatter } from "next-intl";

import { useDay, useMoney } from "./parts";

/**
 * How the Curator's History, Graph health and Runtime pages write their figures, in the visitor's locale: counts with
 * group separators, compact counts for an axis ("12K"), shares as percents, dollars as the hub counts them, durations in
 * the unit that reads best, nights as dates without a time zone shifting them, and moments with their time.
 */
export type InsightFormat = ReturnType<typeof useInsightFormat>;

export function useInsightFormat() {
  const format = useFormatter();
  const money = useMoney();
  const day = useDay();
  const unit = (value: number, name: "millisecond" | "second" | "minute" | "hour" | "kilobyte" | "megabyte" | "gigabyte", digits: number) =>
    format.number(value, { style: "unit", unit: name, unitDisplay: "short", maximumFractionDigits: digits });
  return {
    count: (value: number) => format.number(value, { maximumFractionDigits: 0 }),
    compact: (value: number) => format.number(value, { notation: "compact", maximumFractionDigits: 1 }),
    decimal: (value: number) => format.number(value, { maximumFractionDigits: 2 }),
    percent: (share: number) => format.number(share, { style: "percent", maximumFractionDigits: 1 }),
    money,
    /** Milliseconds as ms, s or min, whichever reads in fewer digits. */
    ms: (value: number) => (value < 1000 ? unit(value, "millisecond", 0) : value < 60_000 ? unit(value / 1000, "second", 1) : unit(value / 60_000, "minute", 1)),
    /** Bytes in kB, MB or GB of 1000. */
    bytes: (value: number) => (value < 1e6 ? unit(value / 1e3, "kilobyte", 1) : value < 1e9 ? unit(value / 1e6, "megabyte", 1) : unit(value / 1e9, "gigabyte", 2)),
    hours: (value: number) => (value < 1 ? unit(value * 60, "minute", 0) : unit(value, "hour", 1)),
    /** A night (YYYY-MM-DD) as the axis names it, and in full. */
    shortDay: (night: string) => {
      const [year, month, date] = night.split("-").map(Number);
      if (!year || !month || !date) return night;
      return format.dateTime(new Date(Date.UTC(year, month - 1, date, 12)), { month: "short", day: "numeric", timeZone: "UTC" });
    },
    day: (night: string) => day(night),
    /** A moment (ISO 8601) as the axis names it, and in full with its time. */
    shortMoment: (at: string) => format.dateTime(new Date(at), { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
    moment: (at: string) => format.dateTime(new Date(at), { dateStyle: "medium", timeStyle: "short" }),
  };
}
