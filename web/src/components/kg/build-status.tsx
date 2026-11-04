"use client";

import { Archive, Network, Recycle } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { Ago } from "@/components/data/ago";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { EmptyState } from "@/components/states/states";
import { buildDurations, shortHash } from "@/lib/kg/format";
import type { KgBuild } from "@/lib/kg/types";

import { BuildStatusBadge, Duration, When } from "./badges";

/**
 * The build side of the knowledge graph page, from GET /v1/kg/{project}/builds (kg_builds and the procrastinate
 * queue): the history and what the other parts share (build-activity.tsx shows the build in flight or the one that
 * just failed, graph-in-use.tsx the graph the pages read). Read-only:
 * builds are queued by pushes (`evo-agents hub kg push`), never from the web.
 *
 * A build's artifact is its graph file in the blob store. A build whose content did not change points at the artifact
 * of an earlier one (`artifact_reused_from`), and the hub's retention deletes the artifacts of older graphs
 * (`artifact_pruned_at`): such a build keeps its content hash and counts, but no page reads it any more.
 */

/** The graph the pages read: the newest successful build that still holds an artifact. */
export function latestSucceeded(builds: readonly KgBuild[]): KgBuild | undefined {
  return builds.find((build) => build.status === "succeeded" && build.artifact_sha256 !== null);
}

export type ArtifactState = "own" | "reused" | "pruned" | "none";

/** What became of a build's artifact. */
export function artifactState(build: KgBuild): ArtifactState {
  if (build.status !== "succeeded") return "none";
  if (build.artifact_pruned_at) return "pruned";
  return build.artifact_reused_from !== null ? "reused" : "own";
}

/** The artifact of a build in a few words: pruned and when, reused from which build, or its own. */
export function ArtifactNote({ build, now }: { build: KgBuild; now: number | null }) {
  const t = useTranslations("kg.artifact");
  const state = artifactState(build);
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5" data-testid="build-artifact" data-state={state}>
      {state === "none" ? <span className="text-muted-foreground">-</span> : null}
      {state === "pruned" ? (
        <>
          <Archive className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span>
            {t("pruned")} <When iso={build.artifact_pruned_at} now={now} />
          </span>
        </>
      ) : null}
      {state === "reused" ? (
        <>
          <Recycle className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span>{t("reused", { id: build.artifact_reused_from ?? 0 })}</span>
        </>
      ) : null}
      {state === "own" ? <span>{t("own")}</span> : null}
    </span>
  );
}

const NARROW = {
  wait: "hidden md:table-cell",
  run: "hidden sm:table-cell",
  nodes: "hidden lg:table-cell",
  edges: "hidden lg:table-cell",
  hash: "hidden xl:table-cell",
  artifact: "hidden lg:table-cell",
  queued: "hidden md:table-cell",
};

export function BuildHistory({
  builds,
  now,
  caption,
  empty,
}: {
  builds: KgBuild[];
  now: number | null;
  caption?: string;
  /** One line in place of the rows when there are none (the failed builds of a project that never failed). */
  empty?: ReactNode;
}) {
  const t = useTranslations("kg.history");
  // Builds of a graph that holds chats hide their totals: the columns go when no row has a figure for them.
  const sized = builds.some((build) => build.nodes !== null || build.edges !== null);
  const columns = useMemo(() => {
    const helper = dataTableColumns<KgBuild>();
    return helper.columns([
      helper.accessor("id", {
        header: () => t("build"),
        sortFn: "basic",
        meta: { primary: true },
        cell: (info) => {
          const { error } = info.row.original;
          return (
            <CellMain sub={error ?? undefined} subTitle={error ?? undefined} danger={Boolean(error)} subTestId="build-error-line">
              <span className="tabular-nums">#{info.getValue()}</span>
            </CellMain>
          );
        },
      }),
      helper.accessor("status", {
        header: () => t("status"),
        sortFn: "text",
        cell: (info) => <BuildStatusBadge status={info.getValue()} />,
      }),
      helper.accessor((row) => new Date(row.queued_at), {
        id: "queued",
        header: () => t("queued"),
        sortFn: "datetime",
        cell: (info) => <Ago value={info.row.original.queued_at} never="-" />,
      }),
      helper.accessor((row) => buildDurations(row, now).wait ?? -1, {
        id: "wait",
        header: () => t("wait"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <Duration ms={buildDurations(info.row.original, now).wait} />,
      }),
      helper.accessor((row) => buildDurations(row, now).run ?? -1, {
        id: "run",
        header: () => t("run"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <Duration ms={buildDurations(info.row.original, now).run} />,
      }),
      ...(sized
        ? [
            helper.accessor((row) => row.nodes ?? -1, {
              id: "nodes",
              header: () => t("nodes"),
              sortFn: "basic",
              meta: { numeric: true },
              cell: (info) => <Count value={info.row.original.nodes} />,
            }),
            helper.accessor((row) => row.edges ?? -1, {
              id: "edges",
              header: () => t("edges"),
              sortFn: "basic",
              meta: { numeric: true },
              cell: (info) => <Count value={info.row.original.edges} />,
            }),
          ]
        : []),
      helper.accessor((row) => row.content_hash ?? "", {
        id: "hash",
        header: () => t("hash"),
        enableSorting: false,
        cell: (info) => (
          <span className="font-mono text-xs" title={info.getValue() || undefined}>
            {shortHash(info.getValue())}
          </span>
        ),
      }),
      helper.accessor((row) => artifactState(row), {
        id: "artifact",
        header: () => t("artifact"),
        sortFn: "text",
        cell: (info) => <ArtifactNote build={info.row.original} now={now} />,
      }),
    ]);
  }, [t, now, sized]);
  return (
    <DataTable
      data={builds}
      columns={columns}
      caption={caption ?? t("caption")}
      empty={empty}
      getRowId={(row) => String(row.id)}
      initialSorting={[{ id: "id", desc: true }]}
      columnClassNames={NARROW}
      testId="kg-build-history"
    />
  );
}

function Count({ value }: { value: number | null }) {
  const t = useTranslations("kg.history");
  return value === null ? <span className="text-muted-foreground">-</span> : <>{t("count", { value })}</>;
}

export function NoBuilds() {
  const t = useTranslations("kg.builds");
  return (
    <EmptyState
      icon={Network}
      title={t("emptyTitle")}
      description={t.rich("emptyDescription", { code: (chunks) => <code className="font-mono">{chunks}</code> })}
    />
  );
}
