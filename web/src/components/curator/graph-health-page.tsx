"use client";

import { useQuery } from "@tanstack/react-query";
import { FileWarning, GitCompareArrows, Network, SearchCheck, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { type Metric, MetricStrip } from "@/components/data/metric-strip";
import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { CuratorHeader, CuratorTabs } from "./curator-header";
import { CuratorHeadSkeleton, useCuratorStatus } from "./curator-page";
import { GraphSources, RetrievalPanel, SyncPanel } from "./graph-health-panels";
import { buildRows, coverageOf, evalOf, measureRows, measureTotals, retrievalOf, sourcesOf, syncOf, type SyncState } from "./graph-health-model";
import { figuresQuery, type KgHealthView, kgHealthQuery } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import type { CuratorStatus } from "./queries";
import { TrendCard } from "./trend-card";
import { series, sparkValues, type TrendRow } from "./trend-model";

const HEALTH_COMMAND = "evo-agents hub kg health --project";

/** A strip's sparkline of one series of a trend, oldest first; none with fewer than two values. */
function spark(rows: TrendRow[], key: string, label: string) {
  const values = sparkValues(rows, key);
  return values ? { values, label } : null;
}

/** The worker could not keep the graph fresh: which sources failed and why. A banner only while one did. */
function SyncFailure({ sync }: { sync: SyncState }) {
  const t = useTranslations("curator.graph.sync");
  const failed = sync.sources.filter((source) => source.status === "failed");
  if (sync.failed === 0 && !sync.error) return null;
  return (
    <Banner icon={TriangleAlert} tone="danger" role="alert" title={t("failedTitle", { worker: sync.worker ?? "-", count: Math.max(sync.failed, 1) })} testId="graph-sync-failed">
      <ul className="flex flex-col gap-0.5">
        {sync.error ? <li className="[overflow-wrap:anywhere]">{sync.error}</li> : null}
        {failed.map((source) => (
          <li key={source.source} className="[overflow-wrap:anywhere]">
            <span className="font-mono text-xs text-foreground">{source.source}</span>: {source.error ?? t("noReason")}
          </li>
        ))}
      </ul>
    </Banner>
  );
}

function GraphBody({ project, view, sync, retrieval }: { project: string; view: KgHealthView; sync: SyncState | null; retrieval: ReturnType<typeof retrievalOf> }) {
  const t = useTranslations("curator.graph");
  const format = useInsightFormat();
  const label = (at: string) => ({ tick: format.shortMoment(at), title: format.moment(at) });
  const measures = measureRows(view, label);
  const builds = buildRows(view.builds, label);
  const latest = view.measures[0] ?? null;
  const read = view.measures.find((measure) => measure.error === null) ?? null;
  const totals = measureTotals(sourcesOf(read));
  const buildId = typeof read?.build?.id === "number" ? read.build.id : null;
  const coverage = coverageOf(view.builds[0]?.coverage);
  const score = evalOf(read?.eval);
  if (view.measures.length === 0 && view.builds.length === 0) {
    return (
      <EmptyState
        icon={Network}
        title={t("empty.title")}
        description={
          <>
            <p>{t("empty.description")}</p>
            <p className="mt-2">{t.rich("empty.cli", { command: () => <code className="rounded-xs bg-muted px-1 font-mono text-xs">{`${HEALTH_COMMAND} ${project}`}</code> })}</p>
          </>
        }
        testId="graph-empty"
      />
    );
  }
  const metrics: Metric[] = [
    {
      id: "behind",
      label: t("strip.behind"),
      icon: GitCompareArrows,
      value: totals.behind ?? 0,
      display: totals.behind === null ? "-" : format.count(totals.behind),
      meta: totals.lagHours ? t("strip.behindMeta", { hours: format.hours(totals.lagHours) }) : t("strip.behindNone"),
      series: spark(measures, "behind", t("strip.behindSeries")),
    },
    {
      id: "off",
      label: t("strip.off"),
      icon: FileWarning,
      value: totals.off ?? 0,
      display: totals.off === null ? "-" : format.count(totals.off),
      meta: t("strip.offMeta"),
      series: spark(measures, "off", t("strip.offSeries")),
    },
    {
      id: "resolved",
      label: t("strip.resolved"),
      icon: Network,
      value: coverage?.resolved ?? 0,
      display: coverage?.resolved == null ? "-" : format.percent(coverage.resolved),
      meta:
        coverage?.ambiguous != null && coverage.dangling != null
          ? t("strip.resolvedMeta", { ambiguous: format.percent(coverage.ambiguous), dangling: format.percent(coverage.dangling) })
          : t("strip.noBuild"),
      series: spark(builds, "resolved", t("strip.resolvedSeries")),
    },
    {
      id: "recall",
      label: t("strip.recall"),
      icon: SearchCheck,
      value: score?.recall5 ?? 0,
      display: score?.recall5 == null ? "-" : format.percent(score.recall5),
      meta: score?.recall5 != null ? t("strip.recallMeta", { questions: score.questions ?? 0, mrr: format.decimal(score.mrr ?? 0) }) : (score?.why ?? t("strip.recallNone")),
      series: spark(measures, "recall5", t("strip.recallSeries")),
    },
  ];
  return (
    <>
      {latest?.error ? (
        <Banner icon={TriangleAlert} tone="danger" title={t("measureFailed", { at: format.moment(latest.measured_at) })} testId="graph-measure-failed">
          <span className="[overflow-wrap:anywhere]">{latest.error}</span>
        </Banner>
      ) : null}
      <div className="flex flex-col gap-2">
        <MetricStrip label={t("strip.label")} metrics={metrics} testId="graph-strip" />
        <p className="text-xs text-fg-subtle" data-testid="graph-measured">
          {read ? t("measured", { at: format.moment(read.measured_at), count: view.measures.length }) : t("notMeasured")}
          {buildId !== null ? ` ${t("build", { id: buildId })}` : null}
        </p>
      </div>
      <GraphSources measure={read} />
      <div className="grid min-w-0 gap-5 lg:grid-cols-2">
        <TrendCard
          title={t("trend.freshTitle")}
          headline={t("trend.freshHeadline", { count: measures.length })}
          empty={t("trend.freshEmpty")}
          kind="lines"
          rows={measures}
          series={series([
            { key: "behind", label: t("trend.behind") },
            { key: "off", label: t("trend.off") },
          ])}
          format={format.count}
          testId="graph-trend-fresh"
        />
        <TrendCard
          title={t("trend.coverageTitle")}
          headline={t("trend.coverageHeadline", { count: builds.length })}
          empty={t("trend.coverageEmpty")}
          kind="lines"
          rows={builds}
          series={series([
            { key: "resolved", label: t("trend.resolved") },
            { key: "ambiguous", label: t("trend.ambiguous") },
            { key: "dangling", label: t("trend.dangling") },
          ])}
          format={format.percent}
          share
          testId="graph-trend-coverage"
        />
        <TrendCard
          title={t("trend.evalTitle")}
          headline={t("trend.evalHeadline")}
          empty={t("trend.evalEmpty")}
          kind="lines"
          rows={measures}
          series={series([
            { key: "recall5", label: t("trend.recall5") },
            { key: "mrr", label: t("trend.mrr") },
          ])}
          format={format.decimal}
          tick={format.percent}
          share
          testId="graph-trend-eval"
        />
        <RetrievalPanel project={project} retrieval={retrieval} />
      </div>
      {sync ? <SyncPanel sync={sync} /> : null}
    </>
  );
}

/**
 * The knowledge graph's health over time (`/p/{project}/curator/graph`): how far each git source lags its repo's
 * default branch and which files the graph lacks, holds stale or still holds after their deletion, what each build
 * resolved, how retrieval scores on the project's own plan steps, what agents got from the graph in their traces, and
 * what the worker on duty said of keeping it fresh. A banner shows only when something failed: the worker's sync, or
 * the latest measure.
 */
export function GraphHealthPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("curator.graph");
  const status = useCuratorStatus(project, initialError);
  const health = useHubQuery(kgHealthQuery(browserApi, project));
  const figures = useQuery(figuresQuery(browserApi, project));
  const held = (figures.data?.figures ?? {}) as Record<string, unknown>;
  const sync = syncOf(held.kg_sync);
  return (
    <QueryView state={status} loading={<CuratorHeadSkeleton />}>
      {(found: CuratorStatus) => (
        <div className="flex flex-col gap-6" data-testid="graph-page">
          <CuratorHeader project={project} status={found} />
          <CuratorTabs project={project} waiting={found.open_proposals} />
          {sync ? <SyncFailure sync={sync} /> : null}
          <QueryView state={health} loading={<ListSkeleton metrics={4} rows={4} />}>
            {(view) =>
              view === null ? (
                <EmptyState icon={Network} title={t("hidden.title")} description={t("hidden.description")} testId="graph-hidden" />
              ) : (
                <GraphBody project={project} view={view} sync={sync} retrieval={retrievalOf(held.kg_health)} />
              )
            }
          </QueryView>
        </div>
      )}
    </QueryView>
  );
}
