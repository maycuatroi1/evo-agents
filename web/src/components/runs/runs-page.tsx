"use client";

import { CircleX, Clock, Eye, Loader2, type LucideIcon, Play, SearchX, Send } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { Pager } from "@/components/admin/pager";
import { usePagedQuery } from "@/components/admin/use-paged-query";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { SearchField } from "@/components/data/search-field";
import { useNow } from "@/components/kg/use-now";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, PageSkeleton, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { STATE_LOOK } from "./badges";
import { DispatchDialog } from "./dispatch-dialog";
import { useCanDispatch, useDispatchedNotice, useViewer } from "./hooks";
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
import { hasActiveRuns, MAX_QUERY, runsQuery, runsSummaryQuery } from "./queries";
import { RunsTable } from "./runs-table";

/** The filters in the URL, changed through the History API (Next.js syncs useSearchParams with it). */
function useFilters(): [RunFilters, (next: RunFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readFilters(params);
  const set = (next: RunFilters) => window.history.replaceState(null, "", `${pathname}${filtersSearch(next)}`);
  return [filters, set];
}

function Stat({ icon: Icon, label, value, hint, testId, spin }: { icon: LucideIcon; label: string; value: number; hint: string; testId: string; spin?: boolean }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border bg-card px-4 py-3.5" data-testid={testId}>
      <dt className="flex items-center gap-1.5 text-sm text-muted-foreground">
        <Icon className={spin && value > 0 ? "size-4 shrink-0 animate-spin motion-reduce:animate-none" : "size-4 shrink-0"} aria-hidden="true" />
        {label}
      </dt>
      <dd className="text-2xl font-semibold tabular-nums" data-value={value}>
        {value}
      </dd>
      <dd className="text-xs text-pretty text-muted-foreground">{hint}</dd>
    </div>
  );
}

function Summary({ summary }: { summary: RunsSummary }) {
  const t = useTranslations("runs.summary");
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
    <section aria-label={t("label")}>
      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="runs-summary">
        <Stat
          icon={Loader2}
          spin
          label={t("running")}
          value={summary.held}
          hint={t("runningHint", { workers: summary.holdingWorkers })}
          testId="summary-running"
        />
        <Stat icon={Clock} label={t("queued")} value={summary.queued} hint={queuedHint} testId="summary-queued" />
        <Stat icon={Eye} label={t("review")} value={summary.review} hint={reviewHint} testId="summary-review" />
        <Stat
          icon={CircleX}
          label={t("failed")}
          value={summary.failed}
          hint={t("failedHint", { lost: summary.lost, cancelled: summary.cancelled })}
          testId="summary-failed"
        />
      </dl>
    </section>
  );
}

const FACET_ICONS: Record<RunFacet, LucideIcon> = {
  active: STATE_LOOK.running.icon,
  review: STATE_LOOK.review.icon,
  done: STATE_LOOK.done.icon,
  ended: STATE_LOOK.failed.icon,
};

function RunList({ project }: { project: string }) {
  const t = useTranslations("runs");
  const viewer = useViewer();
  const [filters, setFilters] = useFilters();
  const { state, stale } = usePagedQuery(runsQuery(browserApi, project, listQuery(filters)));

  return (
    <section aria-labelledby="runs-list-title" className="flex flex-col gap-4">
      <h2 id="runs-list-title" className="sr-only">
        {t("caption", { project })}
      </h2>
      <div className="flex flex-col gap-3 rounded-xl border bg-card p-4">
        <SearchField
          value={filters.q}
          onCommit={(q) => setFilters({ ...filters, q, page: 1 })}
          label={t("search.label")}
          placeholder={t("search.placeholder")}
          clearLabel={t("search.clear")}
          maxLength={MAX_QUERY}
          className="sm:max-w-sm"
          testId="runs-search"
        />
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
          countLabel={(count) => t("count", { count })}
          testId="runs-facets"
        />
      </div>
      <QueryView state={state} loading={<TableSkeleton rows={6} />}>
        {(list) => {
          const live = hasActiveRuns(list);
          return (
            <div className="flex flex-col gap-3" aria-busy={stale || undefined}>
              <p aria-live="polite" className="text-sm text-muted-foreground" data-testid="runs-list-summary">
                {isFiltered(filters) ? t("listSummary.filtered", { count: list.total }) : t("listSummary.all", { count: list.total })}
                {live ? <> {t("live")}</> : null}
              </p>
              {list.runs.length === 0 ? (
                <EmptyState icon={SearchX} title={t("noResults.title")} description={t("noResults.description")}>
                  <Button variant="outline" size="lg" onClick={() => setFilters(NO_FILTERS)}>
                    {t("noResults.clear")}
                  </Button>
                </EmptyState>
              ) : (
                <RunsTable runs={list.runs} caption={t("caption", { project })} viewer={viewer} />
              )}
              <Pager
                label={t("pagerLabel")}
                page={filters.page}
                count={list.runs.length}
                hasNext={list.offset + list.limit < list.total}
                canGoBack={filters.page > 1}
                atStart={filters.page === 1}
                busy={stale}
                onNext={() => setFilters({ ...filters, page: filters.page + 1 })}
                onPrevious={() => setFilters({ ...filters, page: Math.max(1, filters.page - 1) })}
                onFirst={() => setFilters({ ...filters, page: 1 })}
              />
            </div>
          );
        }}
      </QueryView>
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
  const summary = useHubQuery(runsSummaryQuery(browserApi, project), initialError);
  const [dispatching, setDispatching] = useState(false);
  const { notice, show, clear } = useNotice();
  const dispatched = useDispatchedNotice();
  const live = summary.status === "success" && hasActiveRuns(summary.data);

  return (
    <>
      <PageHeader
        eyebrow={<span className="font-mono normal-case">{project}</span>}
        title={t("title")}
        description={t("description")}
        meta={
          <>
            {live ? (
              <Badge variant="info" data-testid="runs-live">
                <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {t("liveBadge")}
              </Badge>
            ) : null}
            {canDispatch ? (
              <Button size="lg" onClick={() => setDispatching(true)} data-testid="runs-dispatch">
                <Send aria-hidden="true" />
                {t("dispatchButton")}
              </Button>
            ) : null}
          </>
        }
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      <QueryView state={summary} loading={<PageSkeleton />}>
        {(active) =>
          facetCount(active.counts, null) === 0 ? (
            // No run in the project yet, in any state: what a run is, and how to start one.
            <EmptyState
              icon={Play}
              title={t("empty.title")}
              description={canDispatch ? t("empty.description") : t("empty.readerDescription")}
            >
              {canDispatch ? (
                <Button size="lg" onClick={() => setDispatching(true)} data-testid="runs-empty-dispatch">
                  <Send aria-hidden="true" />
                  {t("dispatchButton")}
                </Button>
              ) : null}
            </EmptyState>
          ) : (
            <div className="flex flex-col gap-6">
              <Summary summary={summarizeRuns(active)} />
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
          onDispatched={(runs) => show(dispatched(runs))}
        />
      ) : null}
    </>
  );
}
