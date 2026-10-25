"use client";

import { useFormatter, useTranslations } from "next-intl";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { runTiming } from "./model";
import { isActiveState, type Run } from "./queries";
import { useDuration } from "./run-cost-duration";

/** The runs table's cells that read a run's clock, its start and duration; its cost cell is run-cost-duration.tsx's. */

export function Timing({ run, part }: { run: Run; part: "started" | "duration" }) {
  const t = useTranslations("runs");
  const format = useFormatter();
  const duration = useDuration();
  const now = useNow(isActiveState(run.state));
  const timing = runTiming(run, now);
  if (part === "started") {
    const at = timing.startedAt ?? run.queued_at;
    const date = new Date(at);
    return (
      <time
        dateTime={at}
        title={format.dateTime(date, { dateStyle: "medium", timeStyle: "medium" })}
        className={cn("whitespace-nowrap", !timing.startedAt && "text-fg-subtle")}
      >
        {timing.startedAt ? null : <span className="sr-only">{t("queuedAt")} </span>}
        {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
      </time>
    );
  }
  if (timing.durationMs === null) return <span className="text-fg-subtle">-</span>;
  return (
    <span className={cn("whitespace-nowrap", timing.waiting && "text-fg-subtle")} data-testid="run-duration">
      {timing.waiting ? t("waiting", { duration: duration(timing.durationMs) }) : duration(timing.durationMs)}
    </span>
  );
}
