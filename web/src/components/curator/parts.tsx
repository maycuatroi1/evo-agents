"use client";

import { useFormatter } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** A link inside a line of text: `brand` with a quiet underline, so colour is not all that sets it apart (axe). */
export const TEXT_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

/**
 * Small pieces the Curator's pages share: a list of facts (term over value on a phone, side by side from the sm
 * breakpoint) and money as the hub counts it, in US dollars.
 */

export function Facts({ children, className, testId }: { children: ReactNode; className?: string; testId?: string }) {
  return (
    <dl className={cn("grid grid-cols-1 gap-x-4 gap-y-2.5 px-4 py-3 text-sm sm:grid-cols-[minmax(8rem,max-content)_minmax(0,1fr)]", className)} data-testid={testId}>
      {children}
    </dl>
  );
}

export function Fact({ label, children, testId }: { label: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-xs leading-5 text-muted-foreground sm:text-[13px]">{label}</dt>
      <dd className="-mt-2 min-w-0 text-foreground [overflow-wrap:anywhere] sm:mt-0" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

/** "$1.25", or "$0.0042" for a cost under a cent, so a cheap run never reads as free. */
export function useMoney() {
  const format = useFormatter();
  return (value: number) =>
    format.number(value, {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: value > 0 && value < 0.01 ? 4 : 2,
    });
}

/** A day the hub names as YYYY-MM-DD (a night), as people read it, without a time zone shifting it. */
export function useDay() {
  const format = useFormatter();
  return (day: string, style: "medium" | "long" = "medium") => {
    const [year, month, date] = day.split("-").map(Number);
    if (!year || !month || !date) return day;
    return format.dateTime(new Date(Date.UTC(year, month - 1, date, 12)), { dateStyle: style, timeZone: "UTC" });
  };
}
