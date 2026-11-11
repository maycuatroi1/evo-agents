"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Network } from "lucide-react";
import Link from "next/link";
import { useId } from "react";
import { useTranslations } from "next-intl";

import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { type PlanStep, percent } from "@/lib/plans";
import { systemObserveHref, systemsQuery } from "@/lib/systems-queries";
import { cn } from "@/lib/utils";

import { planRunPhase } from "./model";
import type { PlanTrace } from "./plan-run-choice";

const TONE = {
  queued: "border-border bg-card",
  running: "border-running/30 bg-running-soft",
  waiting: "border-attention/30 bg-attention-soft",
  parked: "border-border bg-muted/60",
  review: "border-review/30 bg-review-soft",
} as const;

/**
 * The plan handed to a system, on the plan's page: "Running in system X", the state of the trace's active run, the
 * steps done of the total, and a link to the trace in the system's Observe. The trace comes from ready-steps, which
 * the page polls while it is active.
 */
export function PlanTraceBanner({ project, trace, steps }: { project: string; trace: PlanTrace; steps: readonly PlanStep[] }) {
  const t = useTranslations("runs.planRun.trace");
  const tBanner = useTranslations("runs.planRun.banner");
  const tPhase = useTranslations("runs.planRun.phase");
  const ids = useId();
  const systems = useQuery(systemsQuery(browserApi, project));
  const name = systems.data?.find((system) => system.slug === trace.system)?.name || trace.system;
  const phase = planRunPhase(trace.state);
  const state = tBanner(`state.${phase}`);
  const done = steps.filter((step) => step.group === "done").length;
  const total = steps.length;
  return (
    <section
      aria-labelledby={`${ids}-title`}
      className={cn("flex flex-col gap-3 rounded-md border p-4", TONE[phase])}
      data-testid="plan-trace-banner"
      data-trace-id={trace.id}
      data-system={trace.system}
      data-phase={phase}
    >
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h2 id={`${ids}-title`} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[15px] leading-[22px] font-semibold">
            <Network className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 [overflow-wrap:anywhere]">{t("title", { system: name })}</span>
            <StatusBadge kind="run" status={phase} label={tPhase(`${phase}Long`)} data-testid="plan-trace-phase" />
          </h2>
          <p className="text-sm text-pretty text-muted-foreground">{t("text", { id: trace.id, state })}</p>
        </div>
        <Button asChild variant="outline" className="shrink-0 self-start bg-card">
          <Link href={systemObserveHref(project, trace.system, trace.id)} data-testid="plan-trace-banner-link">
            {t("open")}
            <ArrowRight aria-hidden="true" />
          </Link>
        </Button>
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-sm">
          <span className="font-medium tabular-nums" data-testid="plan-trace-banner-progress">
            {t("progress", { done, total })}
          </span>
          <span className="text-xs text-muted-foreground tabular-nums">{tBanner("percent", { percent: percent(done, total) })}</span>
        </div>
        <div
          className="h-1.5 overflow-hidden rounded-full bg-border"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={done}
          aria-label={tBanner("progressLabel")}
        >
          <div className="h-full rounded-full bg-running transition-[width] duration-slow motion-reduce:transition-none" style={{ width: `${percent(done, total)}%` }} />
        </div>
      </div>
      <p className="sr-only" aria-live="polite">
        {t("live", { system: name, id: trace.id, state, done, total })}
      </p>
    </section>
  );
}
