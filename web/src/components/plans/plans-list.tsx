"use client";

import { useQuery } from "@tanstack/react-query";
import { Archive, ClipboardList, Rocket, Search } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { useCanDispatch, usePlanRunNotice } from "@/components/runs/hooks";
import { activePlanRun, planRunPhase } from "@/components/runs/model";
import { RunPlanButton } from "@/components/runs/plan-run";
import { type Run, runHref, runsSummaryQuery } from "@/components/runs/queries";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NotFoundState, TableSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { plansQuery } from "@/lib/plan-queries";
import { type PlanArea, type PlanSummary, percent } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { planHref } from "./links";
import { ReadOnlyNotice } from "./plan-header";
import { CompactProgress } from "./progress";

const NARROW_HIDDEN = { revision: "hidden md:table-cell", updated: "hidden lg:table-cell" };

/** The project's active runs, as the summary query reads them: which plans have a plan run, or runs of their steps. */
type Activity = { runs: Run[]; loaded: boolean };

/** A plan's active plan run as a badge with an icon and a word, linked to the run. */
function PlanRunLink({ project, run }: { project: string; run: Run }) {
  const t = useTranslations("runs.planRun");
  const phase = planRunPhase(run.state);
  return (
    <Link
      href={runHref(project, run.id)}
      className="w-fit rounded-full transition-[filter] hover:brightness-95 dark:hover:brightness-125"
      aria-label={t("listLink", { id: run.id, state: t(`phase.${phase}Long`) })}
      data-testid="plan-run-link"
      data-run-id={run.id}
    >
      <StatusBadge kind="run" status={phase} data-testid="plan-run-phase" />
    </Link>
  );
}

function matches(plan: PlanSummary, needle: string): boolean {
  if (!needle) return true;
  return [plan.plan_id, plan.title ?? ""].some((value) => value.toLocaleLowerCase("vi").includes(needle));
}

function PlansTable({
  project,
  plans,
  area,
  activity,
  onDispatched,
}: {
  project: string;
  plans: PlanSummary[];
  area: PlanArea;
  activity: Activity;
  onDispatched: (run: Run) => void;
}) {
  const t = useTranslations("plans.list");
  const format = useFormatter();
  const canDispatch = useCanDispatch(project);
  const withActions = area === "active" && canDispatch;
  const columns = useMemo(() => {
    const helper = dataTableColumns<PlanSummary>();
    const actions = helper.display({
      id: "actions",
      header: () => <span className="sr-only">{t("columns.actions")}</span>,
      cell: (info) => {
        const plan = info.row.original;
        return (
          <RunPlanButton
            project={project}
            planId={plan.plan_id}
            pending={plan.steps_total - plan.steps_done}
            runs={activity.runs}
            loaded={activity.loaded}
            onDispatched={onDispatched}
            size="sm"
            variant="outline"
            layout="row"
            testId="plans-run-plan"
          />
        );
      },
    });
    const all = helper.columns([
      helper.accessor((row) => row.title ?? row.plan_id, {
        id: "plan",
        header: () => t("columns.plan"),
        sortFn: "text",
        cell: (info) => {
          const plan = info.row.original;
          const planRun = activePlanRun(activity.runs, plan.plan_id);
          return (
            <div className="flex min-w-0 flex-col items-start gap-0.5">
              <Link
                href={planHref(project, plan.plan_id)}
                className={cn(NAME_LINK, "text-pretty")}
                data-testid="plan-link"
              >
                {plan.title ?? plan.plan_id}
              </Link>
              {plan.title ? <span className="font-mono text-xs text-muted-foreground">{plan.plan_id}</span> : null}
              {planRun ? <PlanRunLink project={project} run={planRun} /> : null}
            </div>
          );
        },
      }),
      helper.accessor((row) => percent(row.steps_done, row.steps_total), {
        id: "progress",
        header: () => t("columns.progress"),
        sortFn: "basic",
        cell: (info) => <CompactProgress done={info.row.original.steps_done} total={info.row.original.steps_total} />,
      }),
      helper.accessor("revision", {
        header: () => t("columns.revision"),
        sortFn: "basic",
        cell: (info) => <span className="font-mono text-xs tabular-nums">{info.getValue()}</span>,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        cell: (info) => (
          <div className="flex flex-col gap-0.5 text-xs">
            <time dateTime={info.row.original.updated_at} className="tabular-nums">
              {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="font-mono text-muted-foreground">{info.row.original.updated_by}</span>
          </div>
        ),
      }),
    ]);
    return withActions ? [...all, actions] : all;
  }, [t, format, project, activity, onDispatched, withActions]);

  return (
    <DataTable
      data={plans}
      columns={columns}
      caption={t(area === "active" ? "activeCaption" : "completedCaption")}
      getRowId={(row) => row.plan_id}
      initialSorting={[{ id: "updated", desc: true }]}
      columnClassNames={NARROW_HIDDEN}
      testId={`plans-table-${area}`}
      empty={t("noMatch")}
    />
  );
}

function AreaSection({
  project,
  area,
  plans,
  filtered,
  activity,
  onDispatched,
}: {
  project: string;
  area: PlanArea;
  plans: PlanSummary[];
  filtered: PlanSummary[];
  activity: Activity;
  onDispatched: (run: Run) => void;
}) {
  const t = useTranslations("plans.list");
  const Icon = area === "active" ? Rocket : Archive;
  const id = `plans-${area}-title`;
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3" data-testid={`plans-${area}`}>
      <h2 id={id} className="flex items-center gap-2 text-base font-medium">
        <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
        {t(area === "active" ? "active" : "completed")}
        <Badge variant="secondary" className="font-mono tabular-nums">
          {plans.length}
        </Badge>
      </h2>
      {plans.length === 0 ? (
        <p className="rounded-md border border-dashed bg-card px-4 py-6 text-center text-sm text-muted-foreground">
          {t(area === "active" ? "noActive" : "noCompleted")}
        </p>
      ) : (
        <PlansTable project={project} plans={filtered} area={area} activity={activity} onDispatched={onDispatched} />
      )}
    </section>
  );
}

function Plans({ project, plans }: { project: string; plans: PlanSummary[] }) {
  const t = useTranslations("plans.list");
  const [query, setQuery] = useState("");
  const summary = useQuery(runsSummaryQuery(browserApi, project));
  const runs = summary.data?.runs;
  const activity = useMemo<Activity>(() => ({ runs: runs ?? [], loaded: !summary.isPending }), [runs, summary.isPending]);
  const { notice, show, clear } = useNotice();
  const dispatched = usePlanRunNotice();
  const onDispatched = useMemo(() => (run: Run) => show(dispatched(run)), [show, dispatched]);
  const needle = query.trim().toLocaleLowerCase("vi");
  if (plans.length === 0) {
    return <EmptyState icon={ClipboardList} title={t("emptyTitle")} description={t("emptyDescription")} />;
  }
  const byArea = (area: PlanArea) => plans.filter((plan) => plan.area === area);
  const shown = plans.filter((plan) => matches(plan, needle));
  return (
    <>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="relative w-full sm:max-w-xs">
          <Search
            className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("search")}
            className="h-10 pl-8"
            data-testid="plans-search"
          />
        </div>
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {needle ? t("shown", { shown: shown.length, total: plans.length }) : null}
        </p>
      </div>
      <NoticeArea notice={notice} onDismiss={clear} />
      {(["active", "completed"] as const).map((area) => (
        <AreaSection
          key={area}
          project={project}
          area={area}
          plans={byArea(area)}
          filtered={shown.filter((plan) => plan.area === area)}
          activity={activity}
          onDispatched={onDispatched}
        />
      ))}
    </>
  );
}

export function PlansList({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("plans");
  const tStates = useTranslations("states");
  const state = useHubQuery(plansQuery(browserApi, project), initialError);
  const counts =
    state.status === "success"
      ? {
          active: state.data.filter((plan) => plan.area === "active").length,
          completed: state.data.filter((plan) => plan.area === "completed").length,
        }
      : null;

  if (state.status === "error" && state.error.status === 404) {
    return (
      <NotFoundState
        title={tStates("projectNotFoundTitle", { name: project })}
        description={tStates("projectNotFoundDescription")}
      />
    );
  }
  return (
    <>
      <PageHeader
        title={t("list.title")}
        tags={
          counts ? (
            <>
              <Badge variant="secondary" data-testid="plans-active-count">
                <Rocket aria-hidden="true" />
                {t("list.activeCount", { count: counts.active })}
              </Badge>
              <Badge variant="secondary" data-testid="plans-completed-count">
                <Archive aria-hidden="true" />
                {t("list.completedCount", { count: counts.completed })}
              </Badge>
            </>
          ) : null
        }
      />
      <ReadOnlyNotice />
      <QueryView state={state} loading={<TableSkeleton rows={4} />}>
        {(plans) => <Plans project={project} plans={plans} />}
      </QueryView>
    </>
  );
}
