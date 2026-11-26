"use client";

import { useQuery } from "@tanstack/react-query";
import { ClipboardList } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTimeZone, useTranslations } from "next-intl";
import { type ComponentType, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { DataCard } from "@/components/data/data-card";
import { NewPlanButton } from "@/components/runs/author-run";
import { planActivityLink } from "@/components/runs/plan-run";
import { IDLE_REFRESH_MS, LIVE_REFRESH_MS, type Run, runsSummaryQuery } from "@/components/runs/queries";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults, NotFoundState, TableSkeleton } from "@/components/states/states";
import { useIsMobile } from "@/hooks/use-mobile";
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
import { PlanSelectionBar } from "./plan-selection";
import { PlansToolbar } from "./plans-toolbar";

/**
 * What the peek panel gets: the plan to show (null while it slides shut), its row's summary, and where focus goes back
 * when it closes. The panel joins the plans and the runs features, so the route under src/app hands it in.
 */
export type PeekProps = {
  project: string;
  planId: string | null;
  summary: PlanSummary | null;
  onClose: () => void;
  returnFocus: (planId: string) => HTMLElement | null;
};

/** What the route hands the list: the peek panel, and whether the member may abandon or close plans. */
export type PlansSlots = { Peek: ComponentType<PeekProps> | null; canWrite: boolean };

const NO_SLOTS: PlansSlots = { Peek: null, canWrite: false };

/** What the rows share: the run marks, the ticked plans, and the peek panel's opener. */
type RowsProps = {
  project: string;
  runs: Run[];
  selected: ReadonlySet<string>;
  onSelect: (planId: string, selected: boolean) => void;
  onPeek: ((planId: string) => void) | null;
  peeked: string | null;
};

/** The link of a plan's row, where focus goes back when the peek panel closes. */
function rowLink(planId: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-testid="plan-row"][data-plan-id="${CSS.escape(planId)}"] [data-testid="plan-link"]`);
}

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
function Rows({ project, plans, runs, labelledBy, selected, onSelect, onPeek, peeked }: RowsProps & { plans: PlanSummary[]; labelledBy?: string }) {
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
          selected={selected.has(plan.plan_id)}
          onSelect={onSelect}
          onPeek={onPeek}
          peeked={peeked === plan.plan_id}
        />
      ))}
    </ul>
  );
}

function GroupSection({ group, by, index, ...rows }: RowsProps & { group: PlanGroup; by: PlanListFilters["group"]; index: number }) {
  const t = useTranslations("plans.list");
  const id = `plans-group-${index}`;
  const title = group.key ?? t(`groupNone.${by === "none" ? "problem" : by}`);
  return (
    <section className="border-b last:border-b-0" data-testid="plans-group-section" data-group={group.key ?? ""}>
      <h3 id={id} className="flex min-h-9 items-center gap-2 border-b bg-surface-sunken px-4 py-1.5 text-[13px] leading-[18px] font-semibold">
        <span className={group.key ? "font-mono" : undefined}>{title}</span>
        <span className="font-normal text-fg-subtle tabular-nums">{group.plans.length}</span>
      </h3>
      <Rows {...rows} plans={group.plans} labelledBy={id} />
    </section>
  );
}

function Plans({ project, plans, slots }: { project: string; plans: PlanSummary[]; slots: PlansSlots }) {
  const t = useTranslations("plans.list");
  const tStates = useTranslations("states.noResults");
  const [filters, setFilters] = useFilters();
  const summary = useQuery(runsSummaryQuery(browserApi, project));
  const runs = useMemo(() => summary.data?.runs ?? [], [summary.data?.runs]);
  const inView = useMemo(() => filterPlans(plans, { ...DEFAULT_FILTERS, view: filters.view }), [plans, filters.view]);
  const shown = useMemo(() => sortPlans(filterPlans(plans, filters), filters.sort), [plans, filters]);
  const groups = useMemo(() => groupPlans(shown, filters.group), [shown, filters.group]);
  const phone = useIsMobile();
  const { Peek, canWrite } = slots;
  // The panel mounts once a plan is first peeked and stays, so it can slide shut and give focus back.
  const [peekUsed, setPeekUsed] = useState(false);
  if (filters.plan !== null && !phone && !peekUsed) setPeekUsed(true);
  const [ticked, setTicked] = useState<ReadonlySet<string>>(() => new Set());
  const onSelect = useCallback((planId: string, on: boolean) => {
    setTicked((current) => {
      const next = new Set(current);
      if (on) next.add(planId);
      else next.delete(planId);
      return next;
    });
  }, []);
  const peekRef = useLatest(filters, setFilters);
  const onPeek = useCallback((planId: string) => peekRef.current.set({ ...peekRef.current.filters, plan: planId }), [peekRef]);
  // Only plans the list still holds stay ticked: one abandoned moves to Completed and drops out of Open.
  const selection = shown.filter((plan) => ticked.has(plan.plan_id));
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
  const peeked = phone ? null : filters.plan;
  const rows: RowsProps = { project, runs, selected: ticked, onSelect, onPeek: phone ? null : onPeek, peeked };
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
    body = <Rows {...rows} plans={shown} />;
  } else {
    body = groups.map((group, index) => <GroupSection key={group.key ?? ""} {...rows} group={group} by={filters.group} index={index} />);
  }
  return (
    <DataCard toolbar={toolbar}>
      <div data-testid="plans-list" data-layout="list" data-view={filters.view} aria-label={t(filters.view === "open" ? "openCaption" : "completedCaption")} role="region">
        {body}
      </div>
      <PlanSelectionBar project={project} plans={selection} canWrite={canWrite} onKeep={(ids) => setTicked(new Set(ids))} />
      {Peek && peekUsed && !phone ? (
        <Peek
          project={project}
          planId={peeked}
          summary={plans.find((plan) => plan.plan_id === peeked) ?? null}
          onClose={() => setFilters({ ...filters, plan: null })}
          returnFocus={rowLink}
        />
      ) : null}
    </DataCard>
  );
}

/** The latest filters and their setter, for a callback that stays the same between renders (the rows are memo). */
function useLatest(filters: PlanListFilters, set: (next: PlanListFilters) => void) {
  const ref = useRef({ filters, set });
  useEffect(() => {
    ref.current = { filters, set };
  });
  return ref;
}

/**
 * A project's plans as one list (web/DESIGN.md, Plans): open plans by default, last changed first, each with its
 * current step and step strip. The page changes no plan; the line under the head says plans are edited from the CLI.
 */
export function PlansList({ project, initialError, slots = NO_SLOTS }: { project: string; initialError: ApiErrorInfo | null; slots?: PlansSlots }) {
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
        {(plans) => <Plans project={project} plans={plans} slots={slots} />}
      </QueryView>
    </>
  );
}
