"use client";

import { Activity, CircleCheck, CircleX, Clock, Eye, type LucideIcon, MonitorPlay, Play, Send } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Pager } from "@/components/admin/pager";
import { usePagedQuery } from "@/components/admin/use-paged-query";
import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { ToolbarFilters } from "@/components/data/filter-sheet";
import { MetricStrip, QuietLine } from "@/components/data/metric-strip";
import { SearchField } from "@/components/data/search-field";
import { useNow } from "@/components/kg/use-now";
import { MONITOR_PATH } from "@/components/monitor/model";
import { PageHeader } from "@/components/shell/page-header";
import { ShortcutKeys, useDispatchShortcut } from "@/components/shell/shortcuts";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, ListSkeleton, NoResults, TableSkeleton } from "@/components/states/states";
import { STATUS_LOOKS } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { DispatchDialog } from "./dispatch-dialog";
import { useCanDispatch, useDispatchedToast, useViewer } from "./hooks";
import {
  facetCount,
  filtersSearch,
  isFiltered,
  listQuery,
  NO_FILTERS,
  readFilters,
  RUN_FACETS,
  type RunFacet,
  type RunFilters,
  type RunsSummary,
  summarizeRuns,
} from "./model";
import { MAX_QUERY, runsQuery, runsSummaryQuery } from "./queries";
import { RunsTable } from "./runs-table";

/** The filters in the URL, changed through the History API (Next.js syncs useSearchParams with it). */
function useFilters(): [RunFilters, (next: RunFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readFilters(params);
  const set = (next: RunFilters) => window.history.replaceState(null, "", `${pathname}${filtersSearch(next)}`);
  return [filters, set];
}

/**
 * How many runs are running, queued, waiting for review and failed, in one strip; while none runs, waits or is parked,
 * one quiet line instead, with Dispatch for a writer.
 */
function Summary({ summary, onDispatch }: { summary: RunsSummary; onDispatch: (() => void) | null }) {
  const t = useTranslations("runs.summary");
  const tRuns = useTranslations("runs");
  const format = useFormatter();
  const now = useNow(summary.oldestQueuedAt !== null);
  const queuedHint = summary.oldestQueuedAt
    ? t("queuedSince", {
        time:
          now === null
            ? format.dateTime(new Date(summary.oldestQueuedAt), { dateStyle: "medium", timeStyle: "short" })
            : format.relativeTime(new Date(summary.oldestQueuedAt), Math.max(now, Date.parse(summary.oldestQueuedAt))),
      })
    : summary.queued > 0
      ? t("queuedMany", { count: summary.queued })
      : t("queuedNone");
  const reviewHint = summary.reviewRun
    ? t("reviewOne", { step: summary.reviewRun.step_key ?? "", plan: summary.reviewRun.plan_id })
    : summary.review > 0
      ? t("reviewMany")
      : t("reviewNone");
  return (
    <MetricStrip
      label={t("label")}
      testId="runs-summary"
      metrics={[
        {
          id: "running",
          label: t("running"),
          icon: Activity,
          live: true,
          tone: "running",
          inFlight: true,
          value: summary.held,
          meta: summary.holdingWorkers === 0 ? t("runningIdle") : t("runningHint", { workers: summary.holdingWorkers }),
        },
        { id: "queued", label: t("queued"), icon: Clock, inFlight: true, value: summary.queued, meta: queuedHint },
        { id: "review", label: t("review"), icon: Eye, tone: "attention", inFlight: true, value: summary.review, meta: reviewHint },
        { id: "failed", label: t("failed"), icon: CircleX, value: summary.failed, meta: t("failedHint", { lost: summary.lost }) },
      ]}
      quiet={
        summary.parked === 0 ? (
          <QuietLine
            icon={CircleCheck}
            title={t("quietTitle")}
            testId="runs-quiet"
            action={
              onDispatch ? (
                <Button variant="secondary" size="sm" onClick={onDispatch} data-testid="runs-quiet-dispatch">
                  <Send aria-hidden="true" />
                  {tRuns("dispatchButton")}
                </Button>
              ) : null
            }
          >
            {t("quietText")}
          </QuietLine>
        ) : undefined
      }
    />
  );
}

const FACET_ICONS: Record<RunFacet, LucideIcon> = {
  active: STATUS_LOOKS.run.running.icon,
  review: STATUS_LOOKS.run.review.icon,
  done: STATUS_LOOKS.run.done.icon,
  ended: STATUS_LOOKS.run.failed.icon,
};

function RunList({ project }: { project: string }) {
  const t = useTranslations("runs");
  const tStates = useTranslations("states.noResults");
  const viewer = useViewer();
  const [filters, setFilters] = useFilters();
  // Live like the summary: while the hub cannot be reached the list keeps what it showed, and the top bar says so.
  const { state, stale } = usePagedQuery(runsQuery(browserApi, project, listQuery(filters)), null, { live: true });
  const caption = t("caption", { project });
  const inUse: ActiveFilter[] = [
    ...(filters.facet ? [{ label: t("facets.label"), value: t(`facets.${filters.facet}`) }] : []),
    ...(filters.q ? [{ label: tStates("search"), value: filters.q }] : []),
  ];

  const count =
    state.status === "success"
      ? isFiltered(filters)
        ? t("listSummary.filtered", { count: state.data.total })
        : t("listSummary.all", { count: state.data.total })
      : undefined;
  const toolbar = (
    <DataToolbar label={caption} count={count} countTestId="runs-list-summary">
      <SearchField
        value={filters.q}
        onCommit={(q) => setFilters({ ...filters, q, page: 1 })}
        label={t("search.label")}
        placeholder={t("search.placeholder")}
        clearLabel={t("search.clear")}
        maxLength={MAX_QUERY}
        className="sm:w-64 max-md:flex-1"
        testId="runs-search"
      />
      <ToolbarFilters
        active={filters.facet ? 1 : 0}
        summary={count}
        onClear={() => setFilters({ ...filters, facet: null, page: 1 })}
        testId="runs-filters"
      >
        <FacetGroup
          label={t("facets.label")}
          options={[
            { value: null, label: t("facets.all"), count: state.status === "success" ? facetCount(state.data.counts, null) : undefined },
            ...RUN_FACETS.map(
              (facet): FacetOption => ({
                value: facet,
                label: t(`facets.${facet}`),
                icon: FACET_ICONS[facet],
                count: state.status === "success" ? facetCount(state.data.counts, facet) : undefined,
              }),
            ),
          ]}
          selected={filters.facet}
          onSelect={(facet) => setFilters({ ...filters, facet: facet as RunFacet | null, page: 1 })}
          countLabel={(n) => t("count", { count: n })}
          testId="runs-facets"
        />
      </ToolbarFilters>
    </DataToolbar>
  );

  return (
    <section aria-labelledby="runs-list-title" className="flex flex-col gap-4">
      <h2 id="runs-list-title" className="sr-only">
        {caption}
      </h2>
      <DataCard
        toolbar={toolbar}
        busy={stale}
        footer={
          state.status === "success" ? (
            <Pager
              label={t("pagerLabel")}
              page={filters.page}
              count={state.data.runs.length}
              hasNext={state.data.offset + state.data.limit < state.data.total}
              canGoBack={filters.page > 1}
              atStart={filters.page === 1}
              busy={stale}
              onNext={() => setFilters({ ...filters, page: filters.page + 1 })}
              onPrevious={() => setFilters({ ...filters, page: Math.max(1, filters.page - 1) })}
              onFirst={() => setFilters({ ...filters, page: 1 })}
            />
          ) : null
        }
      >
        <QueryView state={state} loading={<TableSkeleton rows={6} />}>
          {(list) =>
            list.runs.length === 0 ? (
              <NoResults title={t("noResults.title")} filters={inUse} onClear={() => setFilters(NO_FILTERS)} />
            ) : (
              <RunsTable runs={list.runs} caption={caption} viewer={viewer} />
            )
          }
        </QueryView>
      </DataCard>
    </section>
  );
}

/**
 * The runs of a project: how many are running, queued, waiting for review and failed; the list with its state facets
 * and search, refreshed every 5 seconds while a run is active; and, for a writer, the Dispatch dialog.
 */
export function RunsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("runs");
  const canDispatch = useCanDispatch(project);
  // The page's main query: the top bar says from it whether the page is current (every 5 seconds while a run is active).
  const summary = useHubQuery(runsSummaryQuery(browserApi, project), initialError, { live: true });
  const [dispatching, setDispatching] = useState(false);
  const dispatched = useDispatchedToast();
  // D opens this page's Dispatch, the one its button opens.
  const dispatchKey = useDispatchShortcut(canDispatch ? () => setDispatching(true) : null);

  return (
    <>
      <PageHeader
        title={t("title")}
        actions={
          <>
            {/* Every run in flight of every project, live, side by side: the Monitor. */}
            <Button variant="secondary" asChild>
              <Link href={MONITOR_PATH} data-testid="runs-monitor">
                <MonitorPlay aria-hidden="true" />
                {t("monitorButton")}
              </Link>
            </Button>
            {canDispatch ? (
              <Button onClick={() => setDispatching(true)} aria-keyshortcuts={dispatchKey ? "D" : undefined} data-testid="runs-dispatch">
                <Send aria-hidden="true" />
                {t("dispatchButton")}
                {dispatchKey ? (
                  <ShortcutKeys id="dispatch" className="ml-0.5" keyClassName="border-current bg-transparent text-current opacity-70" />
                ) : null}
              </Button>
            ) : null}
          </>
        }
      />
      <QueryView state={summary} loading={<ListSkeleton metrics={4} />}>
        {(active) =>
          facetCount(active.counts, null) === 0 ? (
            // No run in the project yet, in any state: what a run is, and how to start one.
            <EmptyState
              icon={Play}
              title={t("empty.title")}
              description={canDispatch ? t("empty.description") : t("empty.readerDescription")}
            >
              {canDispatch ? (
                <Button onClick={() => setDispatching(true)} data-testid="runs-empty-dispatch">
                  <Send aria-hidden="true" />
                  {t("dispatchButton")}
                </Button>
              ) : null}
            </EmptyState>
          ) : (
            <div className="flex flex-col gap-6">
              <Summary summary={summarizeRuns(active)} onDispatch={canDispatch ? () => setDispatching(true) : null} />
              <RunList project={project} />
            </div>
          )
        }
      </QueryView>
      {canDispatch ? (
        <DispatchDialog
          project={project}
          open={dispatching}
          onOpenChange={setDispatching}
          onDispatched={dispatched}
        />
      ) : null}
    </>
  );
}
