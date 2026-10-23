"use client";

import { useQuery } from "@tanstack/react-query";
import { ChartLine, Hash, RadioTower } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Fact, Facts } from "@/components/data/facts";
import { type Metric, MetricStrip } from "@/components/data/metric-strip";
import { When } from "@/components/data/when";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { type ProblemMetric, problemRevisionsQuery, type ProjectProblem } from "@/lib/problem-queries";
import { MAX_SIGNALS, signalsHref, signalsQuery } from "@/lib/signal-queries";

import { aggregateOf, isRevisionField, metricFilter, metricPoints, windowOf } from "./problem-model";
import { ProblemStatusBadge } from "./problem-status";

/** How a problem's metric reads: "checkout.conversion, mean over 30 days", with its tenant when it names one. */
export function useProblemMetricText(): (metric: ProblemMetric) => string {
  const t = useTranslations("problems.metric");
  return (metric) => {
    const text = t("text", { key: metric.key, aggregate: metric.aggregate, days: windowOf(metric) });
    return metric.tenant ? t("tenant", { text, tenant: metric.tenant }) : text;
  };
}

/** A card of the problem's page: its h2 with an optional figure or link on the right, then what it holds. */
export function Panel({ id, title, aside, children, testId }: { id: string; title: string; aside?: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <section aria-labelledby={id} className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-2 border-b px-4 py-2">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
        {aside ? <div className="ml-auto flex min-w-0 items-center gap-2 text-[13px] text-fg-subtle">{aside}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** A card's rows while they load: two lines in the shape of a row's title and its second line. */
export function PanelSkeleton() {
  return (
    <div className="flex flex-col gap-2 px-4 py-3">
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-4 w-3/4" />
    </div>
  );
}

/** Who it hurts, when, and how they cope today: the problem said before any solution. */
export function WhatHurts({ problem }: { problem: ProjectProblem }) {
  const t = useTranslations("problems.page");
  return (
    <Panel id="problem-hurts-title" title={t("hurtsTitle")} testId="problem-hurts">
      <Facts>
        <Fact label={t("who")} testId="problem-who">
          {problem.who}
        </Fact>
        <Fact label={t("when")} testId="problem-when">
          {problem.when}
        </Fact>
        <Fact label={t("workaround")} testId="problem-workaround">
          {problem.workaround}
        </Fact>
      </Facts>
    </Panel>
  );
}

/**
 * The metric's figures over its window, read from its key's signals as the hub sums a goal's metric up: the figure its
 * aggregate gives with a sparkline of the values, the latest value and when it came, and how many signals the window
 * holds. Every figure is written in words; the sparkline is for the eye.
 */
function MetricFigures({ project, metric }: { project: string; metric: ProblemMetric }) {
  const t = useTranslations("problems.metric");
  const format = useFormatter();
  const [now] = useState(() => Date.now());
  const signals = useQuery(signalsQuery(browserApi, project, metricFilter(metric, now)));
  if (signals.status === "pending") return <Skeleton className="h-[104px] w-full rounded-md" />;
  if (signals.status === "error") return <p className="text-[13px] text-danger">{t("readFailed")}</p>;
  const points = metricPoints(signals.data, metric, now);
  const days = windowOf(metric);
  const number = (value: number) => format.number(value, { maximumFractionDigits: 4 });
  const figure = aggregateOf(points, metric.aggregate);
  const last = points.at(-1) ?? null;
  const values = points.map((point) => point.value);
  const metrics: Metric[] = [
    {
      id: "figure",
      label: t("figure", { aggregate: metric.aggregate, days }),
      icon: ChartLine,
      value: figure ?? 0,
      display: figure === null ? t("noSignal") : number(figure),
      series:
        points.length > 1
          ? {
              values,
              label: t("seriesLabel", {
                count: points.length,
                first: number(values[0]),
                last: number(values[values.length - 1]),
                low: number(Math.min(...values)),
                high: number(Math.max(...values)),
              }),
            }
          : null,
    },
    {
      id: "latest",
      label: t("latest"),
      icon: Hash,
      value: last?.value ?? 0,
      display: last ? number(last.value) : t("noSignal"),
      meta: last ? format.dateTime(new Date(last.time), { dateStyle: "medium", timeStyle: "short" }) : null,
    },
    {
      id: "signals",
      label: t("signals", { days }),
      icon: RadioTower,
      value: points.length,
      meta: signals.data.length >= MAX_SIGNALS ? t("capped", { max: MAX_SIGNALS }) : null,
    },
  ];
  return <MetricStrip label={t("stripLabel")} metrics={metrics} testId="problem-metric-strip" />;
}

/** How much the problem hurts: its metric's definition and figures, with a link to the key's series; a line when it names none. */
export function MetricCard({ project, problem }: { project: string; problem: ProjectProblem }) {
  const t = useTranslations("problems.metric");
  const metricText = useProblemMetricText();
  const { metric } = problem;
  return (
    <Panel
      id="problem-metric-title"
      title={t("title")}
      testId="problem-metric"
      aside={
        metric ? (
          <Link
            href={signalsHref(project, metric.key, metric.tenant ?? null)}
            className="inline-flex min-h-6 items-center gap-1.5 text-brand transition-colors hover:text-brand-hover"
            data-testid="problem-signals"
          >
            <ChartLine className="size-4" aria-hidden="true" />
            {t("series")}
          </Link>
        ) : null
      }
    >
      <div className="flex flex-col gap-3 px-4 py-4">
        {metric ? (
          <>
            <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="problem-metric-text">
              {metricText(metric)}
            </p>
            <MetricFigures project={project} metric={metric} />
          </>
        ) : (
          <p className="text-[13px] text-muted-foreground">{t("none")}</p>
        )}
      </div>
    </Panel>
  );
}

/** The problem's details beside its page: owner, rank, its revision and who wrote it, its note and why it was closed. */
export function DetailsCard({ problem }: { problem: ProjectProblem }) {
  const t = useTranslations("problems.page");
  return (
    <Panel id="problem-details-title" title={t("detailsTitle")} testId="problem-details">
      <Facts>
        <Fact label={t("owner")} testId="problem-owner">
          <span className="font-mono text-[13px]">{problem.owner}</span>
        </Fact>
        <Fact label={t("rank")} testId="problem-rank">
          <span className="tabular-nums">{problem.rank}</span>
        </Fact>
        <Fact label={t("revision")} testId="problem-revision">
          {t("revisionText", { revision: problem.revision, by: problem.created_by })}
        </Fact>
        <Fact label={t("changed")}>
          <When value={problem.created_at} />
        </Fact>
        {problem.close_reason ? (
          <Fact label={t("closeReason")} testId="problem-close-reason">
            {problem.close_reason}
          </Fact>
        ) : null}
        {problem.note ? <Fact label={t("note")}>{problem.note}</Fact> : null}
      </Facts>
    </Panel>
  );
}

/** Every revision of the problem, newest first: its number, status, who wrote it and when, what it changed and why. */
export function RevisionsCard({ project, problem }: { project: string; problem: ProjectProblem }) {
  const t = useTranslations("problems.history");
  const fields = useTranslations("problems.fields");
  const state = useHubQuery(problemRevisionsQuery(browserApi, project, problem.code));
  return (
    <Panel id="problem-history-title" title={t("title")} testId="problem-history">
      <QueryView state={state} loading={<PanelSkeleton />}>
        {(revisions) => (
          <ol className="divide-y" aria-labelledby="problem-history-title">
            {revisions.map((revision) => {
              const changed = revision.changed.filter(isRevisionField).map((field) => fields(field));
              return (
                <li key={revision.revision} className="flex flex-col gap-1 px-4 py-3" data-testid="problem-revision-row" data-revision={revision.revision}>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{t("revision", { revision: revision.revision })}</span>
                    <ProblemStatusBadge status={revision.status} />
                    <span className="ml-auto text-xs text-fg-subtle">
                      <When value={revision.created_at} short />
                    </span>
                  </div>
                  <p className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
                    {revision.revision === 1 ? t("created", { by: revision.created_by }) : t("changed", { by: revision.created_by, fields: changed.join(", ") || t("nothing") })}
                  </p>
                  {revision.note ? <p className="text-[13px] text-foreground [overflow-wrap:anywhere]">{revision.note}</p> : null}
                </li>
              );
            })}
          </ol>
        )}
      </QueryView>
    </Panel>
  );
}
