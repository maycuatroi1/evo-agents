"use client";

import { Hourglass, ListChecks, type LucideIcon, Network, ScanText } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { shortHash } from "@/lib/kg/format";
import type { GraphRef, KgBuild, KgBuilds } from "@/lib/kg/types";

import { CopyButton, When } from "./badges";
import { latestSucceeded } from "./build-status";

function Tile({
  icon: Icon,
  label,
  children,
  detail,
  testId,
}: {
  icon: LucideIcon;
  label: string;
  children: ReactNode;
  detail?: ReactNode;
  testId?: string;
}) {
  return (
    <Card className="gap-2 py-4" data-testid={testId}>
      <CardHeader className="px-4">
        <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <Icon className="size-4" aria-hidden="true" />
          {label}
        </p>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-1 px-4">
        <div className="min-w-0 text-xl font-semibold tracking-tight tabular-nums">{children}</div>
        {detail ? <div className="text-xs text-muted-foreground">{detail}</div> : null}
      </CardContent>
    </Card>
  );
}

/**
 * The size the Size tile shows: the build's totals, or, when the build hides them (its graph holds chats most members
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

/**
 * Four tiles: the graph the pages read, its size, its content hash, and the queue. `visible` is the graph the reads
 * answer from, counted as the reader sees it; the Size tile falls back to it when the build hides its totals.
 */
export function StatusTiles({ data, now, visible }: { data: KgBuilds; now: number | null; visible?: GraphRef | null }) {
  const t = useTranslations("kg.tiles");
  const graph = latestSucceeded(data.builds);
  const size = shownSize(graph, visible);
  const waiting = data.jobs.filter((job) => job.status === "todo").length;
  const running = data.jobs.filter((job) => job.status === "doing").length;
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" data-testid="kg-tiles">
      <Tile
        icon={Network}
        label={t("graph")}
        testId="tile-graph"
        detail={graph ? <When iso={graph.finished_at} now={now} /> : t("noGraphDetail")}
      >
        {graph ? t("build", { id: graph.id }) : <span className="text-muted-foreground">{t("noGraph")}</span>}
      </Tile>
      <Tile
        icon={ScanText}
        label={t("size")}
        testId="tile-size"
        detail={
          size ? (
            <span data-mine={size.mine ? "true" : "false"}>
              {size.mine ? t("visibleEdges", { count: size.edges }) : t("edges", { count: size.edges })}
            </span>
          ) : undefined
        }
      >
        {size ? t("nodes", { count: size.nodes }) : <span className="text-muted-foreground">-</span>}
      </Tile>
      <Tile icon={ListChecks} label={t("contentHash")} testId="tile-hash" detail={graph ? t("hashDetail") : undefined}>
        {graph?.content_hash ? (
          <span className="flex items-center gap-1">
            <span className="truncate font-mono text-base" title={graph.content_hash}>
              {shortHash(graph.content_hash)}
            </span>
            <CopyButton value={graph.content_hash} label={t("copyHash")} />
          </span>
        ) : (
          <span className="text-muted-foreground">-</span>
        )}
      </Tile>
      <Tile
        icon={Hourglass}
        label={t("queue")}
        testId="tile-queue"
        detail={data.jobs.length > 0 ? t("refreshing") : undefined}
      >
        {data.jobs.length === 0 ? (
          <span className="text-base font-medium text-muted-foreground">{t("queueEmpty")}</span>
        ) : (
          <span className="text-base">{t("queueCounts", { waiting, running })}</span>
        )}
      </Tile>
    </div>
  );
}
