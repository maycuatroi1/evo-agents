"use client";

import { Info } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useSyncExternalStore } from "react";

import { useNow } from "@/components/kg/use-now";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import {
  type CellState,
  countCells,
  heartbeatCells,
  heartbeatsVersion,
  missedRuns,
  observationsOf,
  STRIP_MINUTES,
  subscribeHeartbeats,
} from "./heartbeats";

const CELL: Record<CellState, string> = {
  received: "h-full bg-success-solid",
  missed: "h-2/5 bg-destructive",
  unknown: "h-1 bg-neutral-solid",
  before: "h-px bg-border",
};

const noVersion = () => -1;

/** The kit's compact bars (Fleet on Home): a full bar received, a short one missed, a 3 px stub not watched. */
const BAR: Record<CellState, string> = {
  received: "h-full bg-success-solid",
  missed: "h-2 bg-danger-solid",
  unknown: "h-[3px] bg-neutral-solid",
  before: "h-px bg-border",
};

/**
 * The kit's HeartbeatStrip at its compact size, for the Fleet card on Home: 60 bars of the last hour, 18 px high, from
 * the same observations as the worker page's strip. The bars are drawn for the eye; the strip is one image named with
 * the counts, so a screen reader hears "30 with a heartbeat, 0 without, 30 not recorded".
 */
export function HeartbeatBars({ workerId, createdAt, className }: { workerId: number; createdAt: string; className?: string }) {
  const t = useTranslations("workers.heartbeat");
  const now = useNow(true);
  const version = useSyncExternalStore(subscribeHeartbeats, heartbeatsVersion, noVersion);
  if (now === null || version < 0) return <Skeleton className={cn("h-[18px] w-full max-w-[298px] rounded-xs", className)} aria-hidden="true" />;
  const cells = heartbeatCells(observationsOf(workerId), Date.parse(createdAt), now);
  const counts = countCells(cells);
  const summary = t("summary", {
    received: counts.received,
    missed: counts.missed,
    unknown: counts.unknown + counts.before,
    minutes: STRIP_MINUTES,
  });
  return (
    <div
      role="img"
      aria-label={summary}
      title={summary}
      className={cn("grid h-[18px] w-full max-w-[298px] grid-cols-[repeat(60,minmax(0,1fr))] items-end gap-0.5", className)}
      data-testid="heartbeat-bars"
      data-received={counts.received}
      data-missed={counts.missed}
    >
      {cells.map((cell) => (
        <span key={cell.start} className={cn("block w-full rounded-[1px]", BAR[cell.state])} data-state={cell.state} />
      ))}
    </div>
  );
}

/**
 * One cell per minute of the last hour, from what this tab saw (see heartbeats.ts). A received minute is a full
 * bar, a missed one a short red bar, a minute nobody watched a dot on the baseline: shape and colour both tell them
 * apart, and the counts are written out beside the strip.
 */
export function HeartbeatStrip({ workerId, createdAt }: { workerId: number; createdAt: string }) {
  const t = useTranslations("workers.heartbeat");
  const format = useFormatter();
  const now = useNow(true);
  const version = useSyncExternalStore(subscribeHeartbeats, heartbeatsVersion, noVersion);
  if (now === null || version < 0) {
    return (
      <div className="flex flex-col gap-2" aria-hidden="true">
        <Skeleton className="h-7 w-full" />
        <Skeleton className="h-4 w-48" />
      </div>
    );
  }
  const cells = heartbeatCells(observationsOf(workerId), Date.parse(createdAt), now);
  const counts = countCells(cells);
  const lastRun = missedRuns(cells).at(-1);
  const time = (instant: number) => format.dateTime(new Date(instant), { timeStyle: "short" });
  const stateLabel = (state: CellState) => t(`states.${state}`);
  const summary = t("summary", {
    received: counts.received,
    missed: counts.missed,
    unknown: counts.unknown + counts.before,
    minutes: STRIP_MINUTES,
  });

  return (
    <div className="flex flex-col gap-3" data-testid="heartbeat-strip">
      <div
        role="img"
        aria-label={summary}
        className="grid h-8 grid-cols-[repeat(60,minmax(0,1fr))] items-end gap-px sm:gap-0.5"
        data-received={counts.received}
        data-missed={counts.missed}
      >
        {cells.map((cell) => (
          <span key={cell.start} className="flex h-full items-end" title={`${time(cell.start)}: ${stateLabel(cell.state)}`}>
            <span className={cn("block w-full rounded-[2px]", CELL[cell.state])} data-state={cell.state} />
          </span>
        ))}
      </div>
      <div className="flex justify-between text-xs text-muted-foreground" aria-hidden="true">
        <span>{t("axisStart", { minutes: STRIP_MINUTES })}</span>
        <span>{t("axisEnd")}</span>
      </div>
      <p className="text-sm tabular-nums" data-testid="heartbeat-summary">
        {summary}
        {lastRun ? <> {t("lastMissed", { minutes: lastRun.minutes, time: time(lastRun.start) })}</> : null}
      </p>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground" aria-label={t("legend")}>
        {(["received", "missed", "unknown"] as const).map((state) => (
          <li key={state} className="flex items-center gap-1.5">
            <span className="flex h-3 w-2 items-end" aria-hidden="true">
              <span className={cn("block w-full rounded-[1px]", CELL[state])} />
            </span>
            {stateLabel(state)}
          </li>
        ))}
      </ul>
      <p className="flex items-start gap-2 text-xs text-pretty text-muted-foreground">
        <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        {t("note")}
      </p>
    </div>
  );
}
