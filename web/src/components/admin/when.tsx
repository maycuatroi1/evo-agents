"use client";

import { useFormatter } from "next-intl";

/** A time, or "never" for a token not used yet. `short` fits dense tables; the full time is its tooltip. */
export function When({ value, never, short = false }: { value: string | null | undefined; never?: string; short?: boolean }) {
  const format = useFormatter();
  if (!value) return <span className="text-muted-foreground">{never}</span>;
  const date = new Date(value);
  const full = format.dateTime(date, { dateStyle: "medium", timeStyle: "short" });
  return (
    <time dateTime={value} title={short ? full : undefined} className="whitespace-nowrap tabular-nums">
      {short ? format.dateTime(date, { dateStyle: "short", timeStyle: "short" }) : full}
    </time>
  );
}
