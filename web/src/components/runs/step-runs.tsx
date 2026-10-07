"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleDashed, Loader2, Play } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { useLiveQuery } from "@/components/live/live-context";
import { ShortcutKeys, useDispatchShortcut } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { DispatchDialog, PlanRunHoldNote, useNotReadyReason } from "./dispatch-dialog";
import { useCanDispatch, useDispatchedToast, useViewer } from "./hooks";
import { hasActiveRuns, LIVE_REFRESH_MS, readyStepsQuery, runsHref, runsQuery, stepRunsQuery } from "./queries";
import { RunsTable } from "./runs-table";

/**
 * The runs of one plan step on the step's page: whether the step is ready to run, the Run this step button for a
 * writer (it opens the Dispatch dialog with the step picked), and the step's latest runs, refreshed every 5 seconds
 * while one is active.
 */
export function StepRuns({ project, planId, stepKey }: { project: string; planId: string; stepKey: string }) {
  const t = useTranslations("runs.step");
  const ids = useId();
  const canDispatch = useCanDispatch(project);
  const viewer = useViewer();
  const runsOptions = runsQuery(browserApi, project, stepRunsQuery(planId, stepKey));
  const runs = useQuery(runsOptions);
  const live = hasActiveRuns(runs.data);
  // The step's runs are the work in progress on its page: the top bar follows them while one is active.
  useLiveQuery(runsOptions, true);
  // While a run of the step, or the plan's plan run, is active, its end can make the step ready again: ask as often as
  // the list does.
  const ready = useQuery({
    ...readyStepsQuery(browserApi, project, planId),
    refetchInterval: (query) => (live || query.state.data?.plan_run ? LIVE_REFRESH_MS : false),
  });
  const reason = useNotReadyReason();
  const [dispatching, setDispatching] = useState(false);
  const dispatched = useDispatchedToast();
  const readiness = ready.data?.steps.find((step) => step.key === stepKey) ?? null;
  const isReady = readiness?.ready === true;
  // While the plan has a plan run, every step is that run's: Run this step stays locked, and says which run holds it.
  const planRun = ready.data?.plan_run ? { planId: ready.data.plan_id, run: ready.data.plan_run } : null;
  // D runs this step while it is ready; otherwise D opens the project's Dispatch, as on the project's other pages.
  const dispatchKey = useDispatchShortcut(canDispatch && isReady ? () => setDispatching(true) : null);

  return (
    <section className="flex flex-col gap-3 rounded-md border bg-card shadow-raised p-4" aria-labelledby={`${ids}-title`} data-testid="step-runs">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
            {t("title")}
          </h2>
          {ready.isPending ? (
            <Skeleton className="h-4 w-48" />
          ) : readiness ? (
            <p
              id={`${ids}-readiness`}
              className={cn("flex items-start gap-1.5 text-sm", isReady ? "text-success" : "text-muted-foreground")}
              data-testid="step-readiness"
              data-ready={isReady}
            >
              {isReady ? (
                <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              ) : (
                <CircleDashed className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              )}
              <span className="text-pretty">{isReady ? t("ready") : t("notReady", { reason: reason(readiness, planRun) })}</span>
            </p>
          ) : null}
          {!canDispatch ? <p className="text-xs text-muted-foreground">{t("readerHint")}</p> : null}
        </div>
        {canDispatch ? (
          <Button
            type="button"
            className="shrink-0 aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
            aria-disabled={!isReady || undefined}
            aria-describedby={readiness ? `${ids}-readiness` : undefined}
            aria-keyshortcuts={dispatchKey ? "D" : undefined}
            onClick={() => {
              if (isReady) setDispatching(true);
            }}
            data-testid="step-run"
            data-locked={planRun ? "planRun" : undefined}
          >
            <Play aria-hidden="true" />
            {t("run")}
            {dispatchKey ? (
              <ShortcutKeys id="dispatch" className="ml-0.5" keyClassName="border-current bg-transparent text-current opacity-70" />
            ) : null}
          </Button>
        ) : null}
      </div>
      {planRun ? <PlanRunHoldNote project={project} hold={planRun} testId="step-plan-run" /> : null}
      {runs.isPending ? (
        <Skeleton className="h-24 w-full" />
      ) : runs.data === undefined ? ( // a failed refetch keeps the runs it read; the top bar says they are not current
        <p className="text-sm text-danger" role="alert">
          {t("failed")}
        </p>
      ) : runs.data.runs.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="step-runs-empty">
          {t("empty")}
        </p>
      ) : (
        <>
          <RunsTable runs={runs.data.runs} caption={t("caption", { step: stepKey })} viewer={viewer} variant="step" testId="step-runs-table" />
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            {live ? (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {t("live")}
              </span>
            ) : null}
            {runs.data.total > runs.data.runs.length ? (
              <span>{t("more", { shown: runs.data.runs.length, total: runs.data.total })}</span>
            ) : null}
            <Link href={runsHref(project)} className="font-medium text-brand underline-offset-4 hover:underline">
              {t("all")}
            </Link>
          </p>
        </>
      )}
      {canDispatch ? (
        <DispatchDialog
          project={project}
          open={dispatching}
          onOpenChange={setDispatching}
          onDispatched={dispatched}
          plan={planId}
          step={stepKey}
        />
      ) : null}
    </section>
  );
}
