"use client";

import { useQuery } from "@tanstack/react-query";
import { ClipboardList } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTimeZone, useTranslations } from "next-intl";
import { useMemo, useState, useSyncExternalStore } from "react";

import { DataCard } from "@/components/data/data-card";
import { NewPlanButton } from "@/components/runs/author-run";
import { planActivityLink } from "@/components/runs/plan-run";
import { IDLE_REFRESH_MS, LIVE_REFRESH_MS, type Run, runsSummaryQuery } from "@/components/runs/queries";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults, NotFoundState, TableSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import {
  calendarDay,
  DEFAULT_FILTERS,
  filterChoices,
  filterPlans,
  groupPlans,
  type PlanGroup,
  type PlanListFilters,
  readFilters,
  sortPlans,
  writeFilters,
} from "@/lib/plan-list";
import { plansQuery } from "@/lib/plan-queries";
import type { PlanSummary } from "@/lib/plans";

import { PlanRow } from "./plan-row";
import { PlansToolbar } from "./plans-toolbar";

/**
 * The filters, read from the address when the page opens and written back to it through the History API, so a reload
 * or a shared link shows the same list. Only the toolbar changes them, so the page keeps them in its own state.
 */
function useFilters(): [PlanListFilters, (next: PlanListFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const [filters, setFilters] = useState(() => readFilters(new URLSearchParams(params.toString())));
  const set = (next: PlanListFilters) => {
    setFilters(next);
    window.history.replaceState(null, "", `${pathname}${writeFilters(next)}`);
  };
  return [filters, set];
}

function everyMinute(notify: () => void) {
  const timer = setInterval(notify, 60_000);
  return () => clearInterval(timer);
}

/**
 * Today in the page's time zone, read again each minute; the rows change only when the day does. Null on the server
 * and during hydration, so both renders write the same dates.
 */
function useToday(timeZone: string): string | null {
  return useSyncExternalStore(everyMinute, () => calendarDay(Date.now(), timeZone), () => null);
}

/** The rows of one group, each with the mark of its active plan run or system trace. */
function Rows({ project, plans, runs, labelledBy }: { project: string; plans: PlanSummary[]; runs: Run[]; labelledBy?: string }) {
  const timeZone = useTimeZone() ?? "UTC";
  const today = useToday(timeZone);
  return (
    <ul className="divide-y" aria-labelledby={labelledBy}>
      {plans.map((plan) => (
        <PlanRow
          key={plan.plan_id}
          project={project}
          plan={plan}
          activity={planActivityLink(project, plan.plan_id, plan.trace, runs)}
          today={today}
          timeZone={timeZone}
        />
      ))}
    </ul>
  );
}

function GroupSection({ project, group, by, runs, index }: { project: string; group: PlanGroup; by: PlanListFilters["group"]; runs: Run[]; index: number }) {
  const t = useTranslations("plans.list");
  const id = `plans-group-${index}`;
  const title = group.key ?? t(`groupNone.${by === "none" ? "problem" : by}`);
  return (
    <section className="border-b last:border-b-0" data-testid="plans-group-section" data-group={group.key ?? ""}>
      <h3 id={id} className="flex min-h-9 items-center gap-2 border-b bg-surface-sunken px-4 py-1.5 text-[13px] leading-[18px] font-semibold">
        <span className={group.key ? "font-mono" : undefined}>{title}</span>
        <span className="font-normal text-fg-subtle tabular-nums">{group.plans.length}</span>
      </h3>
      <Rows project={project} plans={group.plans} runs={runs} labelledBy={id} />
    </section>
  );
}

function Plans({ project, plans }: { project: string; plans: PlanSummary[] }) {
  const t = useTranslations("plans.list");
  const tStates = useTranslations("states.noResults");
  const [filters, setFilters] = useFilters();
  const summary = useQuery(runsSummaryQuery(browserApi, project));
  const runs = useMemo(() => summary.data?.runs ?? [], [summary.data?.runs]);
  const inView = useMemo(() => filterPlans(plans, { ...DEFAULT_FILTERS, view: filters.view }), [plans, filters.view]);
  const shown = useMemo(() => sortPlans(filterPlans(plans, filters), filters.sort), [plans, filters]);
  const groups = useMemo(() => groupPlans(shown, filters.group), [shown, filters.group]);
  if (plans.length === 0) {
    return (
      <EmptyState icon={ClipboardList} title={t("emptyTitle")} description={t("emptyDescription")}>
        <NewPlanButton project={project} testId="new-plan-empty" />
      </EmptyState>
    );
  }
  const counts = {
    open: plans.filter((plan) => plan.area === "active").length,
    completed: plans.filter((plan) => plan.area === "completed").length,
  };
  const narrowed = shown.length !== inView.length;
  const toolbar = (
    <PlansToolbar
      filters={filters}
      counts={counts}
      choices={filterChoices(inView)}
      shown={narrowed ? t("shown", { shown: shown.length, total: inView.length }) : t("total", { count: inView.length })}
      onChange={setFilters}
    />
  );
  const used = [
    filters.q ? { label: tStates("search"), value: filters.q } : null,
    filters.owner ? { label: t("owner"), value: filters.owner } : null,
    filters.problem ? { label: t("problem"), value: filters.problem } : null,
    filters.repo ? { label: t("repo"), value: filters.repo } : null,
  ].filter((filter) => filter !== null);
  let body;
  if (inView.length === 0) {
    body = (
      <p className="px-4 py-6 text-center text-[13px] text-muted-foreground" data-testid="plans-none">
        {t(filters.view === "open" ? "noOpen" : "noCompleted")}
      </p>
    );
  } else if (shown.length === 0) {
    body = (
      <NoResults
        title={t("noMatchTitle")}
        filters={used}
        onClear={() => setFilters({ ...filters, q: "", owner: null, problem: null, repo: null })}
      />
    );
  } else if (filters.group === "none") {
    body = <Rows project={project} plans={shown} runs={runs} />;
  } else {
    body = groups.map((group, index) => (
      <GroupSection key={group.key ?? ""} project={project} group={group} by={filters.group} runs={runs} index={index} />
    ));
  }
  return (
    <DataCard toolbar={toolbar}>
      <div data-testid="plans-list" data-layout="list" data-view={filters.view} aria-label={t(filters.view === "open" ? "openCaption" : "completedCaption")} role="region">
        {body}
      </div>
    </DataCard>
  );
}

/**
 * A project's plans as one list (web/DESIGN.md, Plans): open plans by default, last changed first, each with its
 * current step and step strip. The page changes no plan; the line under the head says plans are edited from the CLI.
 */
export function PlansList({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("plans");
  const tStates = useTranslations("states");
  // While a system runs one of the plans, its steps move as the system's agents report them: read the list often.
  const state = useHubQuery(
    {
      ...plansQuery(browserApi, project),
      refetchInterval: (query) => (query.state.data?.some((plan) => plan.trace) ? LIVE_REFRESH_MS : IDLE_REFRESH_MS),
    },
    initialError,
  );
  if (state.status === "error" && state.error.status === 404) {
    return (
      <NotFoundState
        title={tStates("projectNotFoundTitle", { name: project })}
        description={tStates("projectNotFoundDescription")}
      />
    );
  }
  const none = state.status === "success" && state.data.length === 0;
  return (
    <>
      <PageHeader
        title={t("list.title")}
        actions={<NewPlanButton project={project} variant={none ? "outline" : "default"} />}
        sub={<span data-testid="plans-cli-note">{t("list.cliNote")}</span>}
      />
      <QueryView state={state} loading={<TableSkeleton rows={4} toolbar />}>
        {(plans) => <Plans project={project} plans={plans} />}
      </QueryView>
    </>
  );
}
