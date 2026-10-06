"use client";

import { CircleCheck, Layers, Loader2, type LucideIcon, Plus, SearchX, Server, WifiOff } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { NAME_LINK } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { useNow } from "@/components/kg/use-now";
import { PageHeader } from "@/components/shell/page-header";
import { STATUS_LOOKS, StatusBadge, useStatusText } from "@/components/status/status-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, PageSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { ChipList } from "./badges";
import { useRecordPrefetched } from "./hooks";
import {
  countByView,
  filtersSearch,
  filterWorkers,
  matchesWorker,
  readFilters,
  readRuntimes,
  summarize,
  WORKER_VIEWS,
  type WorkerFilters,
  type WorkerSummary,
  workerView,
} from "./model";
import { type Worker, workerHref, workerKeys, workersQuery } from "./queries";
import { Ago } from "./ago";
import { RegisterDialog } from "./register-dialog";

const NARROW_HIDDEN = {
  slots: "hidden sm:table-cell",
  runtimes: "hidden sm:table-cell",
  projects: "hidden lg:table-cell",
  owner: "hidden xl:table-cell",
  heartbeat: "hidden md:table-cell",
};

/** The filters in the URL, changed through the History API (Next.js syncs useSearchParams with it). */
function useFilters(): [WorkerFilters, (next: WorkerFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readFilters(params);
  const set = (next: WorkerFilters) => window.history.replaceState(null, "", `${pathname}${filtersSearch(next)}`);
  return [filters, set];
}

function Stat({ icon: Icon, label, value, hint, testId }: { icon: LucideIcon; label: string; value: number; hint: string; testId: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border bg-card shadow-raised px-4 py-3.5" data-testid={testId}>
      <dt className="flex items-center gap-1.5 text-sm text-muted-foreground">
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        {label}
      </dt>
      <dd className="text-2xl font-semibold tabular-nums" data-value={value}>
        {value}
      </dd>
      <dd className="text-xs text-muted-foreground">{hint}</dd>
    </div>
  );
}

function Summary({ summary }: { summary: WorkerSummary }) {
  const t = useTranslations("workers.summary");
  const format = useFormatter();
  const now = useNow(summary.oldestOffline !== null);
  const offlineHint = summary.oldestOffline
    ? t("offlineSince", {
        time:
          now === null
            ? format.dateTime(new Date(summary.oldestOffline), { dateStyle: "medium", timeStyle: "short" })
            : format.relativeTime(new Date(summary.oldestOffline), Math.max(now, Date.parse(summary.oldestOffline))),
      })
    : summary.neverSeen > 0
      ? t("offlineNever", { count: summary.neverSeen })
      : t("offlineNone");
  return (
    <section aria-label={t("label")}>
      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="workers-summary">
        <Stat icon={CircleCheck} label={t("idle")} value={summary.idle} hint={t("idleHint")} testId="summary-idle" />
        <Stat icon={Loader2} label={t("busy")} value={summary.busy} hint={t("busyHint", { runs: summary.heldRuns })} testId="summary-busy" />
        <Stat
          icon={Layers}
          label={t("freeSlots")}
          value={summary.freeSlots}
          hint={t("freeSlotsHint", { total: summary.totalSlots })}
          testId="summary-free-slots"
        />
        <Stat icon={WifiOff} label={t("offline")} value={summary.offline} hint={offlineHint} testId="summary-offline" />
      </dl>
    </section>
  );
}

function WorkersTable({ workers, caption }: { workers: Worker[]; caption: string }) {
  const t = useTranslations("workers");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Worker>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.worker"),
        sortFn: "alphanumeric",
        cell: (info) => {
          const worker = info.row.original;
          return (
            <div className="flex min-w-0 flex-col gap-0.5 py-0.5 whitespace-normal">
              <Link
                href={workerHref(worker.id)}
                className={cn(NAME_LINK, "w-fit font-mono text-sm [overflow-wrap:anywhere]")}
                data-worker-name={worker.name}
              >
                {worker.name}
              </Link>
              <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {t("hostLine", { hostname: worker.hostname, os: worker.os, arch: worker.arch })}
              </span>
            </div>
          );
        },
      }),
      helper.accessor((row) => WORKER_VIEWS.indexOf(workerView(row)), {
        id: "status",
        header: () => t("columns.status"),
        sortFn: "basic",
        cell: (info) => <StatusBadge kind="worker" status={workerView(info.row.original)} />,
      }),
      helper.accessor("held_runs", {
        id: "slots",
        header: () => t("columns.slots"),
        sortFn: "basic",
        cell: (info) => {
          const worker = info.row.original;
          return (
            <span className="font-mono text-xs tabular-nums">
              <span aria-hidden="true">
                {worker.held_runs}/{worker.slots}
              </span>
              <span className="sr-only">{t("slotsLabel", { held: worker.held_runs, slots: worker.slots })}</span>
            </span>
          );
        },
      }),
      helper.display({
        id: "runtimes",
        header: () => t("columns.runtimes"),
        cell: (info) => (
          <ChipList
            items={readRuntimes(info.row.original.runtimes)
              .filter((runtime) => runtime.available !== false)
              .map((runtime) => runtime.name)}
            empty={t("notReported")}
            compact
          />
        ),
      }),
      helper.display({
        id: "projects",
        header: () => t("columns.projects"),
        cell: (info) => <ChipList items={info.row.original.projects} empty="-" compact />,
      }),
      helper.accessor("owner", {
        header: () => t("columns.owner"),
        sortFn: "alphanumeric",
        cell: (info) => <span className="font-mono text-xs">{info.getValue()}</span>,
      }),
      helper.accessor((row) => (row.last_heartbeat_at ? new Date(row.last_heartbeat_at) : new Date(0)), {
        id: "heartbeat",
        header: () => t("columns.heartbeat"),
        sortFn: "datetime",
        cell: (info) => <Ago value={info.row.original.last_heartbeat_at} never={t("never")} />,
      }),
    ]);
  }, [t]);

  return (
    <DataTable
      data={workers}
      columns={columns}
      caption={caption}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN}
      testId="workers-table"
    />
  );
}

function WorkerList({ workers, onRegister }: { workers: Worker[]; onRegister: () => void }) {
  const t = useTranslations("workers");
  const statusText = useStatusText("worker");
  const [filters, setFilters] = useFilters();
  const revoked = countByView(workers).revoked;
  // No live worker, and no filter asking for the revoked ones: the page is empty, with what to do next.
  if (workers.length === revoked && filters.status === null && !filters.q) {
    return (
      <EmptyState icon={Server} title={t("empty.title")} description={t("empty.description")}>
        <Button onClick={onRegister} data-testid="workers-empty-register">
          <Plus aria-hidden="true" />
          {t("registerButton")}
        </Button>
        {revoked > 0 ? (
          <Button variant="outline" onClick={() => setFilters({ status: "revoked", q: "" })}>
            {t("empty.showRevoked")}
          </Button>
        ) : null}
      </EmptyState>
    );
  }
  // Counts follow the search, so each facet says how many of the matching workers it would show.
  const searched = filters.q ? workers.filter((worker) => matchesWorker(worker, filters.q)) : workers;
  const counts = countByView(searched);
  const shown = filterWorkers(workers, filters);
  const hiddenRevoked = filters.status === null ? counts.revoked : 0;
  const options: FacetOption[] = [
    { value: null, label: t("facets.all"), count: searched.length - counts.revoked },
    ...WORKER_VIEWS.map((view) => ({ value: view, label: statusText(view), icon: STATUS_LOOKS.worker[view].icon, count: counts[view] })),
  ];

  return (
    <section aria-labelledby="workers-list-title" className="flex flex-col gap-4">
      <h2 id="workers-list-title" className="sr-only">
        {t("caption")}
      </h2>
      <div className="flex flex-col gap-3 rounded-md border bg-card shadow-raised p-4">
        <SearchField
          value={filters.q}
          onCommit={(q) => setFilters({ ...filters, q })}
          label={t("search.label")}
          placeholder={t("search.placeholder")}
          clearLabel={t("search.clear")}
          debounce={150}
          maxLength={100}
          className="sm:max-w-sm"
          testId="workers-search"
        />
        <FacetGroup
          label={t("facets.label")}
          options={options}
          selected={filters.status}
          onSelect={(status) => setFilters({ ...filters, status: status === null ? null : (status as WorkerFilters["status"]) })}
          countLabel={(count) => t("count", { count })}
          testId="workers-facets"
        />
      </div>
      <p aria-live="polite" className="text-sm text-muted-foreground" data-testid="workers-list-summary">
        {filters.status === null && !filters.q
          ? t("listSummary.all", { count: shown.length })
          : t("listSummary.filtered", { count: shown.length })}
        {hiddenRevoked > 0 ? <> {t("listSummary.revokedHidden", { count: hiddenRevoked })}</> : null}
      </p>
      {shown.length === 0 ? (
        <EmptyState icon={SearchX} title={t("noResults.title")} description={t("noResults.description")}>
          <Button variant="outline" onClick={() => setFilters({ status: null, q: "" })}>
            {t("noResults.clear")}
          </Button>
        </EmptyState>
      ) : (
        <WorkersTable workers={shown} caption={t("caption")} />
      )}
    </section>
  );
}

/** The workers the visitor owns (every worker for a hub admin), refreshed every 10 seconds. */
export function WorkersPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("workers");
  const state = useHubQuery(workersQuery(browserApi), initialError);
  const [registering, setRegistering] = useState(false);
  const { notice, show, clear } = useNotice();
  useRecordPrefetched(workerKeys.list, (data) => data as Worker[]);
  const live = state.status === "success" ? state.data.filter((worker) => worker.status !== "revoked").length : null;

  return (
    <>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
        meta={
          <>
            {live !== null ? <Badge variant="secondary">{t("count", { count: live })}</Badge> : null}
            <Button onClick={() => setRegistering(true)} data-testid="workers-register">
              <Plus aria-hidden="true" />
              {t("registerButton")}
            </Button>
          </>
        }
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(workers) => (
          <div className="flex flex-col gap-6">
            {workers.length > 0 ? <Summary summary={summarize(workers)} /> : null}
            <WorkerList workers={workers} onRegister={() => setRegistering(true)} />
          </div>
        )}
      </QueryView>
      <RegisterDialog open={registering} onOpenChange={setRegistering} onJoined={show} />
    </>
  );
}
