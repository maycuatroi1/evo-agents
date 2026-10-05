"use client";

import { useFormatter } from "next-intl";

import { useNow } from "@/components/kg/use-now";

/**
 * "4 seconds ago" from the browser's clock, ticking; the full time is its tooltip. The server and the first render
 * in the browser show the full time, so the two agree.
 */
export function Ago({ value, never }: { value: string | null; never: string }) {
  const format = useFormatter();
  const now = useNow(value !== null);
  if (!value) return <span className="text-muted-foreground">{never}</span>;
  const date = new Date(value);
  const full = format.dateTime(date, { dateStyle: "medium", timeStyle: "medium" });
  return (
    <time dateTime={value} title={full} className="whitespace-nowrap tabular-nums">
      {now === null ? full : format.relativeTime(date, Math.max(now, date.getTime()))}
    </time>
  );
}
