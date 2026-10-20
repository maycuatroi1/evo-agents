"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, HardDriveUpload, Sigma, TextQuote, TriangleAlert } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { MetricStrip, type Metric } from "@/components/data/metric-strip";
import { Banner } from "@/components/feedback/banner";
import { projectHref } from "@/components/shell/nav";
import { type HubQueryState, QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState } from "@/components/states/states";
import { type RunStatus, STATUS_LOOKS, StatusBadge } from "@/components/status/status-badge";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { CuratorHeader, CuratorTabs } from "./curator-header";
import { CuratorHeadSkeleton, useCuratorStatus } from "./curator-page";
import { ClustersPanel } from "./history-clusters";
import { type Coverage, coverageOf, type HistoryFilters, historySearch, readHistoryFilters } from "./history-model";
import { HistorySearchSection } from "./history-search";
import { HistorySessionsSection } from "./history-sessions";
import {
  clustersQuery,
  figuresQuery,
  type HistoryRunView,
  type HistoryStatus,
  historyStatusQuery,
  type TranscriptUsage,
  transcriptUsageQuery,
} from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { NUMBER, Panel, PanelLine, PanelSkeleton, PanelTable, TABLE_CELL, TABLE_HEAD } from "./panel";
import type { CuratorStatus } from "./queries";

/** The CLI command that sends a machine's transcripts the hub does not hold yet. */
const BACKFILL_COMMAND = "evo-agents hub sessions backfill --project";

/** The filters in the URL, changed through the History API (Next.js syncs useSearchParams with it). */
function useFilters(): [HistoryFilters, (next: HistoryFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readHistoryFilters(params);
  const set = (next: HistoryFilters) => window.history.replaceState(null, "", `${pathname}${historySearch(next)}`);
  return [filters, set];
}

function isRunStatus(state: string): state is RunStatus {
  return Object.hasOwn(STATUS_LOOKS.run, state);
}

/** How much of the history the machines reported the hub holds, what waits for a backfill, and what was read. */
function CoverageStrip({ coverage, history, usage }: { coverage: Coverage | null; history: HistoryStatus | null; usage: TranscriptUsage | null }) {
  const t = useTranslations("curator.history.coverage");
  const format = useInsightFormat();
  const held = coverage?.held ?? usage?.sessions ?? 0;
  const metrics: Metric[] = [
    {
      id: "transcripts",
      label: t("transcripts"),
      icon: TextQuote,
      value: held,
      display: format.count(held),
      meta: coverage?.share != null ? t("transcriptsMeta", { share: format.percent(coverage.share), reported: format.count(coverage.reported) }) : t("transcriptsNone"),
    },
  ];
  if (coverage) {
    metrics.push(
      {
        id: "backfill",
        label: t("backfill"),
        icon: HardDriveUpload,
        tone: "attention",
        value: coverage.waiting,
        display: format.count(coverage.waiting),
        meta: coverage.waiting > 0 ? t("backfillMeta") : t("backfillNone"),
      },
      {
        id: "facets",
        label: t("facets"),
        icon: Sigma,
        value: coverage.analyzed,
        display: format.count(coverage.analyzed),
        extra: t("of", { total: format.count(coverage.analyzable) }),
        meta: coverage.failed > 0 ? t("facetsFailed", { count: coverage.failed }) : t("facetsMeta"),
      },
    );
  }
  if (history) {
    metrics.push({
      id: "model",
      label: t("model"),
      icon: Bot,
      value: history.llm.analyzed,
      display: format.count(history.llm.analyzed),
      extra: t("of", { total: format.count(history.llm.items) }),
      meta: t("modelMeta", { waiting: history.llm.waiting, failed: history.llm.failed }),
    });
  }
  return <MetricStrip label={t("label")} metrics={metrics} testId="history-coverage" />;
}

/** What the transcripts on the hub come to, per member who uploaded them, and how long the hub keeps them. */
function MembersPanel({ state }: { state: { usage: TranscriptUsage | null | undefined; error: ApiErrorInfo | null; retry: () => void } }) {
  const t = useTranslations("curator.history.members");
  const format = useInsightFormat();
  const { usage } = state;
  let body;
  if (state.error) body = <ApiErrorState error={state.error} onRetry={state.retry} />;
  else if (usage === undefined) body = <PanelSkeleton rows={3} />;
  else if (usage === null) body = <PanelLine testId="history-members-hidden">{t("hidden")}</PanelLine>;
  else if (usage.members.length === 0) body = <PanelLine testId="history-members-empty">{t("empty")}</PanelLine>;
  else {
    body = (
      <PanelTable label={t("caption")} testId="history-members-table">
        <thead className="border-b bg-surface-sunken">
          <tr>
            <th scope="col" className={TABLE_HEAD}>
              {t("member")}
            </th>
            <th scope="col" className={`${TABLE_HEAD} text-right`}>
              {t("sessions")}
            </th>
            <th scope="col" className={`${TABLE_HEAD} text-right`}>
              {t("transcripts")}
            </th>
            <th scope="col" className={`${TABLE_HEAD} text-right`}>
              {t("size")}
            </th>
          </tr>
        </thead>
        <tbody>
          {usage.members.map((member) => (
            <tr key={member.login} className="border-b last:border-b-0" data-testid="history-member" data-login={member.login}>
              <th scope="row" className={`${TABLE_CELL} text-left font-mono text-xs font-normal text-foreground`}>
                {member.login}
              </th>
              <td className={`${TABLE_CELL} ${NUMBER}`}>{format.count(member.sessions)}</td>
              <td className={`${TABLE_CELL} ${NUMBER}`}>{format.count(member.transcripts)}</td>
              <td className={`${TABLE_CELL} ${NUMBER}`}>{format.bytes(member.bytes)}</td>
            </tr>
          ))}
        </tbody>
      </PanelTable>
    );
  }
  const sub = usage
    ? [
        t("kept", { days: usage.retention_days }),
        t("stored", { size: format.bytes(usage.bytes), source: format.bytes(usage.source_bytes) }),
        ...(usage.first_at ? [t("since", { at: format.moment(usage.first_at) })] : []),
        ...(usage.deleted > 0 ? [t("deleted", { count: usage.deleted })] : []),
      ].join(" ")
    : undefined;
  return (
    <Panel title={t("title")} sub={sub} testId="history-members">
      {body}
    </Panel>
  );
}

function RunLine({ project, run }: { project: string; run: HistoryRunView }) {
  const t = useTranslations("curator.history.runs");
  const format = useInsightFormat();
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-[13px]" data-testid="history-run" data-state={run.state}>
      <Link href={projectHref(project, `runs/${run.id}`) as Route} className="font-mono font-medium text-foreground tabular-nums hover:text-brand" aria-label={t("open", { id: run.id })}>
        #{run.id}
      </Link>
      {isRunStatus(run.state) ? <StatusBadge kind="run" status={run.state} /> : <span className="text-xs text-muted-foreground">{run.state}</span>}
      <span className="text-muted-foreground">{t("read", { analyzed: run.analyzed, failed: run.failed, named: run.named })}</span>
      <span className="ml-auto text-xs text-fg-subtle tabular-nums">
        {run.cost_usd !== null ? `${format.money(run.cost_usd)}, ` : ""}
        {run.night ? format.day(run.night) : format.moment(run.queued_at)}
      </span>
    </li>
  );
}

/** The night's history run: its cap and model, and its latest runs with what each read. */
function RunsPanel({ project, state }: { project: string; state: HubQueryState<HistoryStatus> }) {
  const t = useTranslations("curator.history.runs");
  const format = useInsightFormat();
  const history = state.status === "success" ? state.data : null;
  let body;
  if (state.status === "loading") body = <PanelSkeleton rows={3} />;
  else if (state.status === "error") body = <ApiErrorState error={state.error} onRetry={state.retry} />;
  else if (state.data.runs.length === 0) body = <PanelLine testId="history-runs-empty">{t("empty")}</PanelLine>;
  else {
    body = (
      <ul className="flex flex-col divide-y" aria-label={t("title")}>
        {state.data.runs.map((run) => (
          <RunLine key={run.id} project={project} run={run} />
        ))}
      </ul>
    );
  }
  const model = history?.model ?? t("cheapest");
  const sub = !history
    ? undefined
    : history.budget_usd === null
      ? t("noCharter")
      : history.budget_usd === 0
        ? t("off")
        : t("cap", { budget: format.money(history.budget_usd), model, runtime: history.runtime ?? "-" });
  return (
    <Panel title={t("title")} sub={sub} testId="history-runs">
      {body}
    </Panel>
  );
}

function HistoryBody({ project }: { project: string }) {
  const t = useTranslations("curator.history");
  const format = useInsightFormat();
  const [filters, setFilters] = useFilters();
  const history = useHubQuery(historyStatusQuery(browserApi, project));
  const usage = useQuery(transcriptUsageQuery(browserApi, project));
  const figures = useQuery(figuresQuery(browserApi, project));
  const clusters = useHubQuery(clustersQuery(browserApi, project));
  const coverage = coverageOf((figures.data?.figures as Record<string, unknown> | undefined)?.facets);
  const status = history.status === "success" ? history.data : null;
  const failed = (coverage?.failed ?? 0) + (status?.llm.failed ?? 0);
  return (
    <>
      <div className="flex flex-col gap-2">
        <CoverageStrip coverage={coverage} history={status} usage={usage.data ?? null} />
        <p className="text-xs text-fg-subtle" data-testid="history-coverage-when">
          {figures.data ? t("coverage.when", { night: format.day(figures.data.night) }) : figures.isPending ? " " : t("coverage.noFigures")}
        </p>
      </div>
      {coverage && coverage.waiting > 0 ? (
        <Banner icon={HardDriveUpload} tone="attention" title={t("backfill.title", { count: coverage.waiting })} testId="history-backfill">
          {t.rich("backfill.text", {
            command: () => (
              <code className="rounded-xs bg-card px-1 font-mono text-xs text-foreground">
                {BACKFILL_COMMAND} {project}
              </code>
            ),
          })}
        </Banner>
      ) : null}
      {failed > 0 ? (
        <Banner icon={TriangleAlert} tone="danger" title={t("failed.title", { count: failed })} testId="history-failed">
          {t("failed.text")}
        </Banner>
      ) : null}
      <div className="grid min-w-0 items-start gap-4 lg:grid-cols-2">
        <MembersPanel state={{ usage: usage.isPending ? undefined : (usage.data ?? null), error: usage.isError ? usage.error.info : null, retry: () => void usage.refetch() }} />
        <RunsPanel project={project} state={history} />
      </div>
      <HistorySearchSection project={project} filters={filters} onFilters={setFilters} />
      <ClustersPanel project={project} state={clusters} selected={filters.cluster} onSelect={(cluster) => setFilters({ ...filters, cluster, page: 1 })} />
      <HistorySessionsSection project={project} filters={filters} onFilters={setFilters} clusters={clusters.status === "success" ? clusters.data.clusters : []} />
    </>
  );
}

/**
 * A project's history as the Curator reads it (`/p/{project}/curator/history`): how many of the sessions the machines
 * reported the hub holds the transcript of, what waits for a backfill and what the facets and the cheap model have
 * read; the transcripts per member; the history run; the search over every transcript, run log and group chat; the
 * clusters of the sessions; and the sessions and runs themselves, each with its transcript or log a click away. A
 * banner shows only when something needs a person: transcripts a backfill would send, facets that failed.
 */
export function HistoryPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const status = useCuratorStatus(project, initialError);
  return (
    <QueryView state={status} loading={<CuratorHeadSkeleton />}>
      {(found: CuratorStatus) => (
        <div className="flex flex-col gap-6" data-testid="history-page">
          <CuratorHeader project={project} status={found} />
          <CuratorTabs project={project} waiting={found.open_proposals} />
          <HistoryBody project={project} />
        </div>
      )}
    </QueryView>
  );
}
