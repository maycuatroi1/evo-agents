"use client";

import { useQuery } from "@tanstack/react-query";
import { Archive, ClipboardList, Rocket } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { useCanDispatch, usePlanRunNotice } from "@/components/runs/hooks";
import { activePlanRun, planRunPhase } from "@/components/runs/model";
import { RunPlanButton } from "@/components/runs/plan-run";
import { type Run, runHref, runsSummaryQuery } from "@/components/runs/queries";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults, NotFoundState, TableSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
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
      className="shrink-0 rounded-full transition-[filter] hover:brightness-95 dark:hover:brightness-125"
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
      meta: { actions: true },
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
        meta: { primary: true },
        cell: (info) => {
          const plan = info.row.original;
          const planRun = activePlanRun(activity.runs, plan.plan_id);
          const title = plan.title ?? plan.plan_id;
          return (
            <CellMain sub={plan.title ? <span className="font-mono">{plan.plan_id}</span> : null} subTitle={plan.plan_id}>
              <Link
                href={planHref(project, plan.plan_id)}
                className={cn(NAME_LINK, "truncate")}
                title={title}
                data-testid="plan-link"
              >
                {title}
              </Link>
              {planRun ? <PlanRunLink project={project} run={planRun} /> : null}
            </CellMain>
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
        meta: { numeric: true },
        cell: (info) => <Identifier value={t("revisionValue", { revision: info.getValue() })} />,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => (
          <div className="flex flex-col items-end gap-px">
            <time dateTime={info.row.original.updated_at} className="whitespace-nowrap">
              {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="font-mono text-xs leading-4 text-fg-subtle">{info.row.original.updated_by}</span>
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

/** One area of the plans card: its heading with the number of plans, then its table. */
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
    <section aria-labelledby={id} className="border-b last:border-b-0" data-testid={`plans-${area}`}>
      <h2 id={id} className="flex min-h-12 items-center gap-2 border-b px-4 py-2 text-[15px] leading-[22px] font-semibold">
        <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
        {t(area === "active" ? "active" : "completed")}
        <Badge variant="secondary" className="tabular-nums">
          {plans.length}
        </Badge>
      </h2>
      {plans.length === 0 ? (
        <p className="px-4 py-6 text-center text-[13px] text-muted-foreground">
          {t(area === "active" ? "noActive" : "noCompleted")}
        </p>
      ) : (
        <PlansTable project={project} plans={filtered} area={area} activity={activity} onDispatched={onDispatched} />
      )}
    </section>
  );
}

/**
 * The search, read from the URL (`?q=`) when the page opens and written back to it through the History API, so a
 * reload or a shared link shows the same plans. Only this field changes it, so the page keeps it in its own state.
 */
function useSearch(): [string, (q: string) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const [q, setQ] = useState(() => (params.get("q") ?? "").trim().slice(0, 200));
  const set = (next: string) => {
    setQ(next);
    window.history.replaceState(null, "", `${pathname}${next ? `?${new URLSearchParams({ q: next })}` : ""}`);
  };
  return [q, set];
}

function Plans({ project, plans }: { project: string; plans: PlanSummary[] }) {
  const t = useTranslations("plans.list");
  const tStates = useTranslations("states.noResults");
  const [query, setQuery] = useSearch();
  const summary = useQuery(runsSummaryQuery(browserApi, project));
  const runs = summary.data?.runs;
  const activity = useMemo<Activity>(() => ({ runs: runs ?? [], loaded: !summary.isPending }), [runs, summary.isPending]);
  const { notice, show, clear } = useNotice();
  const dispatched = usePlanRunNotice();
  const onDispatched = useMemo(() => (run: Run) => show(dispatched(run)), [show, dispatched]);
  const needle = query.toLocaleLowerCase("vi");
  if (plans.length === 0) {
    return <EmptyState icon={ClipboardList} title={t("emptyTitle")} description={t("emptyDescription")} />;
  }
  const byArea = (area: PlanArea) => plans.filter((plan) => plan.area === area);
  const shown = plans.filter((plan) => matches(plan, needle));
  const toolbar = (
    <DataToolbar
      label={t("title")}
      count={needle ? t("shown", { shown: shown.length, total: plans.length }) : t("total", { count: plans.length })}
    >
      <SearchField
        value={query}
        onCommit={setQuery}
        label={t("search")}
        placeholder={t("searchPlaceholder")}
        clearLabel={t("clearSearch")}
        debounce={150}
        maxLength={200}
        className="sm:w-64"
        testId="plans-search"
      />
    </DataToolbar>
  );
  return (
    <>
      <NoticeArea notice={notice} onDismiss={clear} />
      <DataCard toolbar={toolbar}>
        {shown.length === 0 ? (
          <NoResults
            title={t("noMatchTitle")}
            filters={[{ label: tStates("search"), value: query }]}
            onClear={() => setQuery("")}
          />
        ) : (
          (["active", "completed"] as const).map((area) => (
            <AreaSection
              key={area}
              project={project}
              area={area}
              plans={byArea(area)}
              filtered={shown.filter((plan) => plan.area === area)}
              activity={activity}
              onDispatched={onDispatched}
            />
          ))
        )}
      </DataCard>
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
      <QueryView state={state} loading={<TableSkeleton rows={4} toolbar />}>
        {(plans) => <Plans project={project} plans={plans} />}
      </QueryView>
    </>
  );
}
