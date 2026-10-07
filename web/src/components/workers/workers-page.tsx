"use client";

import { Activity, CircleCheck, Layers, Plus, Server, WifiOff } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { NAME_LINK } from "@/components/data/identifier";
import { MetricStrip } from "@/components/data/metric-strip";
import { notify } from "@/components/feedback/toast";
import { SearchField } from "@/components/data/search-field";
import { useNow } from "@/components/kg/use-now";
import { PageHeader } from "@/components/shell/page-header";
import { STATUS_LOOKS, StatusBadge, useStatusText } from "@/components/status/status-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, ListSkeleton, NoResults } from "@/components/states/states";
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
  // The fleet's state, not work in flight: the strip stays even when no worker is busy, so offline machines still show.
  return (
    <MetricStrip
      label={t("label")}
      testId="workers-summary"
      metrics={[
        { id: "idle", label: t("idle"), icon: CircleCheck, value: summary.idle, meta: t("idleHint") },
        {
          id: "busy",
          label: t("busy"),
          icon: Activity,
          live: true,
          tone: "running",
          value: summary.busy,
          meta: t("busyHint", { runs: summary.heldRuns }),
        },
        {
          id: "free-slots",
          label: t("freeSlots"),
          icon: Layers,
          value: summary.freeSlots,
          meta: t("freeSlotsHint", { total: summary.totalSlots }),
        },
        { id: "offline", label: t("offline"), icon: WifiOff, value: summary.offline, meta: offlineHint },
      ]}
    />
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
        meta: { primary: true },
        cell: (info) => {
          const worker = info.row.original;
          const host = t("hostLine", { hostname: worker.hostname, os: worker.os, arch: worker.arch });
          return (
            <CellMain sub={host} subTitle={host}>
              <Link
                href={workerHref(worker.id)}
                className={cn(NAME_LINK, "truncate font-mono text-[13px]")}
                title={worker.name}
                data-worker-name={worker.name}
              >
                {worker.name}
              </Link>
            </CellMain>
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
        meta: { numeric: true },
        cell: (info) => {
          const worker = info.row.original;
          return (
            <span className="font-mono text-xs">
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
        cell: (info) => <span className="font-mono text-xs text-foreground">{info.getValue()}</span>,
      }),
      helper.accessor((row) => (row.last_heartbeat_at ? new Date(row.last_heartbeat_at) : new Date(0)), {
        id: "heartbeat",
        header: () => t("columns.heartbeat"),
        sortFn: "datetime",
        meta: { numeric: true },
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
  const tStates = useTranslations("states.noResults");
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

  const inUse: ActiveFilter[] = [
    ...(filters.status ? [{ label: t("facets.label"), value: statusText(filters.status) }] : []),
    ...(filters.q ? [{ label: tStates("search"), value: filters.q }] : []),
  ];
  const filtered = filters.status !== null || Boolean(filters.q);
  const count = (
    <>
      {filtered ? t("listSummary.filtered", { count: shown.length }) : t("listSummary.all", { count: shown.length })}
      {hiddenRevoked > 0 ? <>, {t("listSummary.revokedHidden", { count: hiddenRevoked })}</> : null}
    </>
  );

  return (
    <section aria-labelledby="workers-list-title" className="flex flex-col gap-4">
      <h2 id="workers-list-title" className="sr-only">
        {t("caption")}
      </h2>
      <DataCard
        toolbar={
          <DataToolbar label={t("caption")} count={count} countTestId="workers-list-summary">
            <SearchField
              value={filters.q}
              onCommit={(q) => setFilters({ ...filters, q })}
              label={t("search.label")}
              placeholder={t("search.placeholder")}
              clearLabel={t("search.clear")}
              debounce={150}
              maxLength={100}
              className="sm:w-64"
              testId="workers-search"
            />
            <FacetGroup
              label={t("facets.label")}
              options={options}
              selected={filters.status}
              onSelect={(status) => setFilters({ ...filters, status: status === null ? null : (status as WorkerFilters["status"]) })}
              countLabel={(n) => t("count", { count: n })}
              testId="workers-facets"
            />
          </DataToolbar>
        }
      >
        {shown.length === 0 ? (
          <NoResults title={t("noResults.title")} filters={inUse} onClear={() => setFilters({ status: null, q: "" })} />
        ) : (
          <WorkersTable workers={shown} caption={t("caption")} />
        )}
      </DataCard>
    </section>
  );
}

/** The workers the visitor owns (every worker for a hub admin), refreshed every 10 seconds. */
export function WorkersPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("workers");
  // The page's main query: the top bar says from it whether the page is current (every 10 seconds).
  const state = useHubQuery(workersQuery(browserApi), initialError, { live: true });
  const [registering, setRegistering] = useState(false);
  useRecordPrefetched(workerKeys.list, (data) => data as Worker[]);
  const live = state.status === "success" ? state.data.filter((worker) => worker.status !== "revoked").length : null;

  return (
    <>
      <PageHeader
        title={t("title")}
        tags={live !== null ? <Badge variant="secondary">{t("count", { count: live })}</Badge> : null}
        actions={
          <Button onClick={() => setRegistering(true)} data-testid="workers-register">
            <Plus aria-hidden="true" />
            {t("registerButton")}
          </Button>
        }
      />
      <QueryView state={state} loading={<ListSkeleton metrics={4} />}>
        {(workers) => (
          <div className="flex flex-col gap-6">
            {workers.length > 0 ? <Summary summary={summarize(workers)} /> : null}
            <WorkerList workers={workers} onRegister={() => setRegistering(true)} />
          </div>
        )}
      </QueryView>
      <RegisterDialog open={registering} onOpenChange={setRegistering} onJoined={notify} />
    </>
  );
}
