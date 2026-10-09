"use client";

import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { RunRef } from "@/components/data/identifier";
import type { OverviewRun } from "@/components/home/model";
import { CountPill } from "@/components/home/parts";
import { PlanRunKindBadge } from "@/components/runs/badges";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { useMediaQuery, XL_QUERY } from "@/hooks/use-media-query";
import { cn } from "@/lib/utils";

import { hasTile, MAX_TILES, type TileRef } from "./model";
import { RunTime, StepsDone, WorkerName } from "./parts";

/**
 * The Monitor's list: every run in flight of every project of the visitor's grants (the overview's active runs:
 * queued, at work, waiting or parked), one row each with its number, project, the plan's or step's title, its state,
 * worker, time and a plan run's steps. The row is the checkbox's label: checking it puts the run on the grid,
 * unchecking takes it off. From xl the list is a column beside the grid that scrolls inside itself; below, a card above
 * the grid that folds away behind its head.
 */

/** The plan's title for a plan run, the step's for a run of one step; a review run of the Curator may have neither. */
function runTitle(run: OverviewRun): string {
  const plan = run.kind === "plan" || run.step_key === null;
  return plan ? (run.plan_title ?? run.title ?? run.plan_id) : (run.title ?? run.step_key ?? run.plan_id);
}

function FlightRow({ run, watched, locked, onToggle }: { run: OverviewRun; watched: boolean; locked: boolean; onToggle: () => void }) {
  const t = useTranslations("monitor.list");
  const tRuns = useTranslations("runs");
  const ids = useId();
  const title = runTitle(run) || tRuns("untitled");
  const disabled = locked && !watched;
  return (
    <li className="border-t first:border-t-0" data-testid="flight-item" data-run-id={run.id} data-project={run.project} data-watched={watched}>
      <label
        className={cn(
          "grid grid-cols-[16px_minmax(0,1fr)] gap-x-3 px-4 py-2.5 transition-colors",
          disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-accent",
          watched && "bg-surface-selected hover:bg-surface-selected",
        )}
        title={disabled ? t("full", { max: MAX_TILES }) : undefined}
      >
        <input
          type="checkbox"
          checked={watched}
          disabled={disabled}
          onChange={onToggle}
          aria-label={t("watch", { id: run.id })}
          aria-describedby={`${ids}-title ${ids}-facts`}
          className={cn("mt-0.5 size-4 shrink-0 accent-primary", disabled ? "cursor-not-allowed" : "cursor-pointer")}
          data-testid="flight-watch"
        />
        <span className="flex min-w-0 flex-col gap-1">
          <span className="flex min-w-0 items-center gap-2">
            <RunRef id={run.id} className="text-xs" />
            <span className="min-w-0 truncate text-xs text-fg-subtle">{run.project}</span>
            <StatusBadge kind="run" status={run.state} className="ml-auto" />
          </span>
          <span id={`${ids}-title`} className="flex min-w-0 items-center gap-2 text-[13px] leading-[18px] font-medium">
            <span className="truncate" title={title}>
              {title}
            </span>
            {run.kind === "plan" ? <PlanRunKindBadge /> : null}
          </span>
          <span id={`${ids}-facts`} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle">
            <WorkerName worker={run.worker} />
            <RunTime run={run} />
            {run.kind === "plan" && run.steps_total !== null && run.steps_done !== null ? (
              <StepsDone done={run.steps_done} total={run.steps_total} testId="flight-steps" />
            ) : null}
          </span>
        </span>
      </label>
    </li>
  );
}

export function FlightList({
  runs,
  tiles,
  onToggle,
}: {
  runs: readonly OverviewRun[];
  /** What the grid holds now. */
  tiles: readonly TileRef[];
  onToggle: (tile: TileRef) => void;
}) {
  const t = useTranslations("monitor.list");
  const ids = useId();
  // From xl the list is a column that scrolls inside itself, a region Tab reaches; below, the page scrolls it.
  const column = useMediaQuery(XL_QUERY);
  // Below xl the list folds behind its head; it starts open while the grid has nothing to show.
  const [open, setOpen] = useState<boolean | null>(null);
  const shown = open ?? tiles.length === 0;
  const full = tiles.length >= MAX_TILES;

  return (
    <section
      aria-labelledby={`${ids}-title`}
      className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised"
      data-testid="flight-list"
    >
      <div className={cn("flex min-h-12 items-center gap-2 border-b px-4 py-2", !shown && "max-xl:border-b-0")}>
        <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        {runs.length > 0 ? (
          <>
            <CountPill value={runs.length} tone="running" testId="flight-count" />
            <span className="sr-only">{t("count", { count: runs.length })}</span>
          </>
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="ml-auto xl:hidden"
          aria-expanded={shown}
          aria-controls={`${ids}-list`}
          onClick={() => setOpen(!shown)}
          data-testid="flight-toggle"
        >
          {shown ? t("hide") : t("show")}
          <ChevronDown className={cn("transition-transform", shown && "rotate-180")} aria-hidden="true" />
        </Button>
      </div>
      <div id={`${ids}-list`} className={cn("min-h-0 flex-col xl:flex xl:flex-1", shown ? "flex" : "hidden")}>
        {full ? (
          <p className="border-b bg-surface-sunken px-4 py-2 text-xs text-muted-foreground" data-testid="flight-full">
            {t("full", { max: MAX_TILES })}
          </p>
        ) : null}
        {runs.length === 0 ? (
          <p className="px-4 py-6 text-[13px] text-muted-foreground" data-testid="flight-empty">
            {t("empty")}
          </p>
        ) : (
          <ul
            aria-labelledby={`${ids}-title`}
            tabIndex={column ? 0 : undefined}
            className="min-h-0 xl:flex-1 xl:overflow-y-auto xl:overscroll-contain xl:focus-visible:outline-offset-[-2px]"
            data-testid="flight-rows"
          >
            {runs.map((run) => (
              <FlightRow
                key={`${run.project}:${run.id}`}
                run={run}
                watched={hasTile(tiles, run)}
                locked={full}
                onToggle={() => onToggle({ project: run.project, id: run.id })}
              />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
