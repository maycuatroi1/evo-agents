"use client";

import { CircleCheck, CircleDot, Spline, Timer } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { type Metric, MetricStrip } from "@/components/data/metric-strip";
import { buildDurations } from "@/lib/kg/format";
import type { GraphRef, KgBuild } from "@/lib/kg/types";

import { useDurationText } from "./badges";
import { latestSucceeded } from "./build-status";

/**
 * The size the figures show: the build's totals, or, when the build hides them (its graph holds chats most members
 * may not read), the nodes and edges of the graph the reader can read, as GET /v1/kg/{project}/graph counts them.
 */
export function shownSize(
  graph: KgBuild | undefined,
  visible: GraphRef | null | undefined,
): { nodes: number; edges: number; mine: boolean } | null {
  if (!graph) return null;
  if (graph.nodes !== null && graph.edges !== null) return { nodes: graph.nodes, edges: graph.edges, mine: false };
  return visible ? { nodes: visible.nodes, edges: visible.edges, mine: true } : null;
}

/** The middle value of a list of numbers, or null for none. */
export function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2;
}

/** How the latest builds went: those that finished, how many of them succeeded, and the run time of each success. */
export function buildRecord(builds: readonly KgBuild[]) {
  const finished = builds.filter((build) => build.status === "succeeded" || build.status === "failed");
  const succeeded = finished.filter((build) => build.status === "succeeded");
  // Oldest first, as a series reads.
  const runs = succeeded
    .map((build) => buildDurations(build, null).run)
    .filter((run): run is number => run !== null)
    .reverse();
  return { finished: finished.length, succeeded: succeeded.length, failed: finished.length - succeeded.length, runs };
}

/**
 * The graph and its builds in one row of figures: the nodes and edges of the graph in use, how many of the latest
 * builds succeeded (in `attention` when most failed), and the typical run of a successful build with its trend.
 * `visible` is the graph the reads answer from, counted as the reader sees it; the size falls back to it when the build
 * hides its totals.
 */
export function BuildFigures({ builds, visible }: { builds: readonly KgBuild[]; visible?: GraphRef | null }) {
  const t = useTranslations("kg.figures");
  const format = useFormatter();
  const text = useDurationText();
  const size = shownSize(latestSucceeded(builds), visible);
  const record = buildRecord(builds);
  const typical = median(record.runs);
  const minutes = record.runs.map((run) => Math.round(run / 6_000) / 10);
  const metrics: Metric[] = [
    {
      id: "nodes",
      label: t("nodes"),
      icon: CircleDot,
      value: size?.nodes ?? 0,
      display: size ? format.number(size.nodes) : "-",
      meta: size?.mine ? t("readable") : undefined,
    },
    {
      id: "edges",
      label: t("edges"),
      icon: Spline,
      value: size?.edges ?? 0,
      display: size ? format.number(size.edges) : "-",
      meta: size?.mine ? t("readable") : undefined,
    },
    {
      id: "succeeded",
      label: t("succeeded"),
      icon: CircleCheck,
      value: record.succeeded,
      extra: record.finished > 0 ? t("ofFinished", { count: record.finished }) : null,
      meta: record.finished > 0 ? t("failed", { count: record.failed }) : t("noneFinished"),
      tone: record.failed > record.succeeded ? "attention" : undefined,
    },
    {
      id: "build-time",
      label: t("buildTime"),
      icon: Timer,
      value: typical === null ? 0 : Math.max(1, Math.round(typical / 1000)),
      display: typical === null ? "-" : text(typical),
      meta: typical === null ? t("noRuns") : t("median", { count: record.runs.length }),
      series:
        minutes.length > 1 ? { values: minutes, label: t("series", { values: minutes.map((value) => format.number(value)).join(", ") }) } : null,
    },
  ];
  return <MetricStrip label={t("label")} metrics={metrics} testId="kg-figures" />;
}
