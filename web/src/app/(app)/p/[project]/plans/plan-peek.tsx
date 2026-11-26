"use client";

import { useQuery } from "@tanstack/react-query";
import { Ban, Copy, Ellipsis, Maximize2, Play, X } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type RefObject, useRef, useState } from "react";

import { useClipboard } from "@/components/data/identifier";
import { planHref } from "@/components/plans/links";
import { PlanWriteDialog } from "@/components/plans/plan-write-dialog";
import { useCanDispatch, usePlanRunToast } from "@/components/runs/hooks";
import { isCuratorPlan, planRunLock } from "@/components/runs/model";
import { usePlanActivity, useLockText } from "@/components/runs/plan-run";
import { ApiErrorState, LoadingState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import type { PeekProps } from "@/components/plans/plans-list";
import { planName } from "@/lib/plan-list";
import { planQuery } from "@/lib/plan-queries";
import { type Plan, type PlanSummary, parsePlan } from "@/lib/plans";

import { Facts, Goal, Runs, Section, Steps, writtenStatus } from "./peek-sections";

const PlanRunDialog = dynamic(() => import("@/components/runs/plan-run-dialog").then((module) => module.PlanRunDialog), { ssr: false });
const CuratorBuildDialog = dynamic(() => import("@/components/runs/curator-build-dialog").then((module) => module.CuratorBuildDialog), {
  ssr: false,
});

/** Run plan, Abandon and Copy id, in the menu of the panel's head. Run plan and Abandon are a writer's. */
function Actions({ project, plan, name, summary }: { project: string; plan: Plan | null; name: string; summary: PlanSummary | null }) {
  const t = useTranslations("plans.peek");
  const tRun = useTranslations("runs.planRun");
  const planId = plan?.plan_id ?? summary?.plan_id ?? "";
  const canWrite = useCanDispatch(project);
  const lockText = useLockText();
  const toast = usePlanRunToast();
  const activity = usePlanActivity(project, planId, canWrite && planId !== "");
  const { copy } = useClipboard(planId, null);
  const [running, setRunning] = useState(false);
  const [ran, setRan] = useState(false);
  const [abandoning, setAbandoning] = useState(false);
  const steps = plan ? parsePlan(plan.body, plan.plan_id).steps : [];
  const pending = plan ? steps.filter((step) => !["done", "descoped"].includes(writtenStatus(step) ?? "pending")).length : (summary ? summary.steps_total - summary.steps_done : 0);
  const lock = activity.loaded ? planRunLock(activity.active, planId, pending, activity.trace) : "loading";
  const status = plan ? parsePlan(plan.body, plan.plan_id).status : null;
  const abandoned = status === "abandoned" || status === "superseded";
  const revision = plan?.revision ?? summary?.revision ?? 0;
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" size="icon" aria-label={t("actions")} title={t("actions")} data-testid="plan-peek-menu">
            <Ellipsis aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          {canWrite ? (
            <DropdownMenuItem
              disabled={lock !== null}
              onSelect={() => {
                setRan(true);
                setRunning(true);
              }}
              className="flex-col items-start gap-0.5"
              data-testid="plan-peek-run"
            >
              <span className="flex items-center gap-2">
                <Play aria-hidden="true" />
                {tRun("button")}
              </span>
              {lock !== null ? <span className="text-xs text-muted-foreground">{lockText(lock)}</span> : null}
            </DropdownMenuItem>
          ) : null}
          {canWrite && !abandoned ? (
            <DropdownMenuItem variant="destructive" onSelect={() => setAbandoning(true)} data-testid="plan-peek-abandon">
              <Ban aria-hidden="true" />
              {t("abandon")}
            </DropdownMenuItem>
          ) : null}
          {canWrite ? <DropdownMenuSeparator /> : null}
          <DropdownMenuItem onSelect={() => void copy()} data-testid="plan-peek-copy">
            <Copy aria-hidden="true" />
            {t("copyId")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {ran ? (
        isCuratorPlan(planId) ? (
          <CuratorBuildDialog project={project} planId={planId} open={running} onOpenChange={setRunning} onDispatched={toast} />
        ) : (
          <PlanRunDialog project={project} planId={planId} open={running} onOpenChange={setRunning} onDispatched={toast} />
        )
      ) : null}
      <PlanWriteDialog
        project={project}
        action="abandon"
        plans={[{ plan_id: planId, name, revision }]}
        open={abandoning}
        onOpenChange={setAbandoning}
      />
    </>
  );
}

function PeekBody({ project, planId, summary }: { project: string; planId: string; summary: PlanSummary | null }) {
  const t = useTranslations("plans.peek");
  const detail = useQuery(planQuery(browserApi, project, planId));
  const activity = usePlanActivity(project, planId);
  if (detail.isError) {
    if (isApiError(detail.error) && detail.error.info.status === 404) {
      return <p className="px-4 py-6 text-sm text-muted-foreground" data-testid="plan-peek-not-found">{t("notFound", { plan: planId })}</p>;
    }
    return <ApiErrorState error={isApiError(detail.error) ? detail.error.info : { status: 0, code: "network", message: String(detail.error), requestId: null }} onRetry={() => void detail.refetch()} />;
  }
  if (!detail.data) {
    return (
      <LoadingState className="p-4">
        <div className="flex flex-col gap-3">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      </LoadingState>
    );
  }
  const plan = detail.data;
  const view = parsePlan(plan.body, plan.plan_id);
  const done = view.steps.filter((step) => ["done", "descoped"].includes(writtenStatus(step) ?? "pending")).length;
  return (
    <>
      <Section title={t("about")}>
        <Facts plan={plan} summary={summary} project={project} />
      </Section>
      {view.goal ? (
        <Section title={t("goal")}>
          <Goal goal={view.goal} />
        </Section>
      ) : null}
      <Section title={t("steps", { done, total: view.steps.length })}>
        <Steps project={project} planId={plan.plan_id} steps={view.steps} planRun={activity.planRun} />
      </Section>
      <Section title={t("runs")}>
        <Runs project={project} planId={plan.plan_id} />
      </Section>
    </>
  );
}

/** The panel's head: the plan's name and slug, the full page, the menu and Close. */
function PeekHead({ project, planId, summary, headingRef }: { project: string; planId: string; summary: PlanSummary | null; headingRef: RefObject<HTMLHeadingElement | null> }) {
  const t = useTranslations("plans.peek");
  const detail = useQuery(planQuery(browserApi, project, planId));
  const body = detail.data ? parsePlan(detail.data.body, planId) : null;
  const name = summary ? planName(summary) : body ? planName({ plan_id: planId, title: body.title, goal: body.goal }) : planId;
  return (
    <div className="flex shrink-0 items-start gap-1 border-b px-4 py-2.5">
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 py-1">
        <SheetTitle asChild>
          <h2 ref={headingRef} tabIndex={-1} className="text-[15px] leading-[22px] font-semibold text-balance [overflow-wrap:anywhere] focus-visible:outline-none" data-testid="plan-peek-title">
            {name}
          </h2>
        </SheetTitle>
        {name !== planId ? (
          <span className="min-w-0 truncate font-mono text-xs text-fg-subtle" data-testid="plan-peek-slug">
            {planId}
          </span>
        ) : null}
      </div>
      <Button asChild variant="ghost" size="icon">
        <Link href={planHref(project, planId)} aria-label={t("openPage")} title={t("openPage")} data-testid="plan-peek-open">
          <Maximize2 aria-hidden="true" />
        </Link>
      </Button>
      <Actions project={project} plan={detail.data ?? null} name={name} summary={summary} />
      <SheetClose asChild>
        <Button type="button" variant="ghost" size="icon" aria-label={t("close")} title={t("close")} data-testid="plan-peek-close">
          <X aria-hidden="true" />
        </Button>
      </SheetClose>
    </div>
  );
}

/**
 * The peek panel of the plans list (web/DESIGN.md, Plans): a plan at a glance without leaving the list. It slides in
 * from the right, 480 px wide from the sm breakpoint, over a list that stays where it was: the name and slug, the full
 * page, a menu with Run plan, Abandon and Copy id, then who changed the plan last and when, its problems and repos, the
 * goal (cut, with Read all), the steps with where a plan run is, and its latest plan runs. Esc, Close or a click
 * outside call `onClose`; focus then goes to what `returnFocus` names, the plan's row.
 */
export function PlanPeek({ project, planId, summary, onClose, returnFocus }: PeekProps) {
  const t = useTranslations("plans.peek");
  // The plan stays on screen while the panel slides out.
  const [shown, setShown] = useState<string | null>(planId);
  if (planId !== null && planId !== shown) setShown(planId);
  const heading = useRef<HTMLHeadingElement>(null);
  return (
    <Sheet open={planId !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent
        side="right"
        showCloseButton={false}
        aria-describedby={undefined}
        className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-[480px]"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          heading.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          // Radix gives focus back only to a Sheet trigger; the panel opens from a row's link and from the URL.
          event.preventDefault();
          if (shown) returnFocus(shown)?.focus();
        }}
        data-testid="plan-peek"
        data-plan-id={shown ?? undefined}
      >
        {shown ? (
          <>
            <PeekHead key={`head-${shown}`} project={project} planId={shown} summary={summary?.plan_id === shown ? summary : null} headingRef={heading} />
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain" data-testid="plan-peek-body">
              <PeekBody key={shown} project={project} planId={shown} summary={summary?.plan_id === shown ? summary : null} />
            </div>
          </>
        ) : (
          <SheetTitle className="sr-only">{t("titleUnknown")}</SheetTitle>
        )}
      </SheetContent>
    </Sheet>
  );
}
