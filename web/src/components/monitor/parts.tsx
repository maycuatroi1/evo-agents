"use client";

import { useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { ranFor } from "@/components/home/model";
import { Relative, useDuration } from "@/components/home/parts";
import { useNow } from "@/components/kg/use-now";
import { StepBar } from "@/components/plans/progress";
import { isTerminalState, type RunState } from "@/components/runs/queries";

/** What the Monitor's list and tiles say of a run besides its state: its worker, its time and its plan's steps. */

export type TimedRun = { state: RunState; queued_at: string; started_at: string | null; finished_at: string | null };

/**
 * When the run is: queued 3 min ago while no worker started it, "12 min so far" while it goes on (ticking), "ran 12
 * min, ended 2 min ago" once it ended.
 */
export function RunTime({ run }: { run: TimedRun }) {
  const t = useTranslations("monitor.tile");
  const duration = useDuration();
  const ended = isTerminalState(run.state);
  const now = useNow(!ended && run.started_at !== null);
  if (ended) {
    const ms = ranFor(run, null);
    return (
      <>
        {ms !== null ? <span data-testid="run-ran">{t("ran", { duration: duration(ms) })}</span> : null}
        {run.finished_at ? <span>{t.rich("ended", { time: () => <Relative value={run.finished_at ?? ""} /> })}</span> : null}
      </>
    );
  }
  if (run.started_at === null) return <span>{t.rich("queued", { time: () => <Relative value={run.queued_at} /> })}</span>;
  const ms = ranFor(run, now);
  return ms === null ? null : <span data-testid="run-elapsed">{t("elapsed", { duration: duration(ms) })}</span>;
}

/** The worker that holds the run, as a name chip, or that none has claimed it yet. */
export function WorkerName({ worker }: { worker: string | null }) {
  const t = useTranslations("monitor.tile");
  return worker ? <Identifier value={worker} className="h-[18px] max-w-40 truncate" /> : <span>{t("noWorker")}</span>;
}

/** A plan run's steps done of the total, as a bar and "3/11"; the whole sentence for screen readers. */
export function StepsDone({ done, total, testId }: { done: number; total: number; testId: string }) {
  const t = useTranslations("monitor.tile");
  return (
    <span className="inline-flex items-center gap-1.5" data-testid={testId} data-done={done} data-total={total}>
      <StepBar counts={{ done, pending: Math.max(total - done, 0), in_progress: 0, blocked: 0, other: 0, total }} className="h-1.5 w-14 border-0" />
      <span aria-hidden="true" className="tabular-nums">{`${done}/${total}`}</span>
      <span className="sr-only">{t("steps", { done, total })}</span>
    </span>
  );
}
