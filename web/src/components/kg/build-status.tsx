"use client";

import {
  Archive,
  CircleAlert,
  Hourglass,
  ListChecks,
  type LucideIcon,
  Network,
  Recycle,
  ScanText,
  TriangleAlert,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { EmptyState } from "@/components/states/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { buildDurations, shortHash } from "@/lib/kg/format";
import type { KgBuild, KgBuilds } from "@/lib/kg/types";

import { BuildStatusBadge, CopyButton, Duration, JobStatusBadge, When } from "./badges";

/**
 * The build side of the knowledge graph page, from GET /v1/kg/{project}/builds (kg_builds and the procrastinate
 * queue): the graph the pages read, the latest build with its times and error, the jobs still queued, and the
 * history. Read-only: builds are queued by pushes (`evo-agents hub kg push`), never from the web.
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

/** Four tiles: the graph the pages read, its size, its content hash, and the queue. */
export function StatusTiles({ data, now }: { data: KgBuilds; now: number | null }) {
  const t = useTranslations("kg.tiles");
  const graph = latestSucceeded(data.builds);
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
        detail={graph ? t("edges", { count: graph.edges ?? 0 }) : undefined}
      >
        {graph ? t("nodes", { count: graph.nodes ?? 0 }) : <span className="text-muted-foreground">-</span>}
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

function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="font-mono text-xs [overflow-wrap:anywhere]" title={title}>
      {children}
    </span>
  );
}

/** Every field of the newest build, its error included. */
export function LatestBuild({ build, now }: { build: KgBuild; now: number | null }) {
  const t = useTranslations("kg.latest");
  const { wait, run } = buildDurations(build, now);
  return (
    <Card data-testid="kg-latest-build" data-build-id={build.id} data-status={build.status}>
      <CardHeader>
        <CardTitle>
          <h3 className="flex flex-wrap items-center gap-2">
            {t("title", { id: build.id })}
            <BuildStatusBadge status={build.status} />
          </h3>
        </CardTitle>
        <CardDescription>{build.requested_by ? t("requestedBy", { login: build.requested_by }) : t("byPush")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {build.status === "failed" && build.error ? (
          <Alert variant="destructive" data-testid="build-error">
            <CircleAlert aria-hidden="true" />
            <AlertTitle>{t("errorTitle")}</AlertTitle>
            <AlertDescription className="font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]">
              {build.error}
            </AlertDescription>
          </Alert>
        ) : null}
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">{t("queued")}</dt>
          <dd>
            <When iso={build.queued_at} />
          </dd>
          <dt className="text-muted-foreground">{t("wait")}</dt>
          <dd data-testid="build-wait">
            <Duration ms={wait} />
          </dd>
          <dt className="text-muted-foreground">{t("started")}</dt>
          <dd>
            <When iso={build.started_at} />
          </dd>
          <dt className="text-muted-foreground">{t("run")}</dt>
          <dd data-testid="build-run">
            <Duration ms={run} />
          </dd>
          <dt className="text-muted-foreground">{t("finished")}</dt>
          <dd>
            <When iso={build.finished_at} />
          </dd>
          <dt className="text-muted-foreground">{t("runs")}</dt>
          <dd className="tabular-nums">{build.runs ?? "-"}</dd>
          <dt className="text-muted-foreground">{t("size")}</dt>
          <dd className="tabular-nums">
            {build.nodes !== null && build.edges !== null
              ? t("sizeValue", { nodes: build.nodes, edges: build.edges })
              : "-"}
          </dd>
          <dt className="text-muted-foreground">{t("contentHash")}</dt>
          <dd className="flex min-w-0 items-start gap-1">
            {build.content_hash ? (
              <>
                <span data-testid="latest-content-hash">
                  <Mono>{build.content_hash}</Mono>
                </span>
                <CopyButton value={build.content_hash} label={t("copyHash")} />
              </>
            ) : (
              <span className="text-muted-foreground">{t("noHash")}</span>
            )}
          </dd>
          <dt className="text-muted-foreground">{t("artifact")}</dt>
          <dd className="flex min-w-0 flex-col gap-0.5">
            <ArtifactNote build={build} now={now} />
            {build.artifact_sha256 ? (
              <span className="text-xs text-muted-foreground">
                <Mono title={build.artifact_sha256}>{shortHash(build.artifact_sha256)}</Mono>
                {build.artifact_size !== null ? <> ({t("artifactSize", { size: build.artifact_size })})</> : null}
              </span>
            ) : null}
          </dd>
          <dt className="text-muted-foreground">{t("config")}</dt>
          <dd>{build.config_digest ? <Mono title={build.config_digest}>{shortHash(build.config_digest)}</Mono> : "-"}</dd>
        </dl>
      </CardContent>
    </Card>
  );
}

/** The project's build jobs still in the queue: waiting (todo) or running (doing). */
export function BuildQueue({ data, now }: { data: KgBuilds; now: number | null }) {
  const t = useTranslations("kg.queue");
  const builds = new Map(data.builds.map((build) => [build.id, build]));
  return (
    <Card data-testid="kg-queue">
      <CardHeader>
        <CardTitle>
          <h3>{t("title")}</h3>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {data.jobs.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="queue-empty">
            {t("empty")}
          </p>
        ) : (
          <ul className="flex flex-col divide-y rounded-md border" aria-label={t("title")}>
            {data.jobs.map((job) => {
              const build = job.build_id !== null ? builds.get(job.build_id) : undefined;
              const { wait } = build ? buildDurations(build, now) : { wait: null };
              return (
                <li
                  key={job.job_id}
                  className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-2.5 text-sm"
                  data-testid="queue-job"
                  data-status={job.status}
                >
                  <span className="flex items-center gap-2">
                    <JobStatusBadge status={job.status} />
                    <span className="font-medium tabular-nums">{t("job", { id: job.job_id })}</span>
                    {job.build_id !== null ? (
                      <span className="text-muted-foreground tabular-nums">{t("forBuild", { id: job.build_id })}</span>
                    ) : null}
                  </span>
                  {build ? (
                    <span className="text-xs text-muted-foreground">
                      {t("since")} <When iso={build.queued_at} />
                      {job.status === "todo" && wait !== null ? (
                        <>
                          , {t("waited")} <Duration ms={wait} />
                        </>
                      ) : null}
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
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
  const columns = useMemo(() => {
    const helper = dataTableColumns<KgBuild>();
    return helper.columns([
      helper.accessor("id", {
        header: () => t("build"),
        sortFn: "basic",
        cell: (info) => <span className="font-medium tabular-nums">#{info.getValue()}</span>,
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
        cell: (info) => <When iso={info.row.original.queued_at} />,
      }),
      helper.accessor((row) => buildDurations(row, now).wait ?? -1, {
        id: "wait",
        header: () => t("wait"),
        sortFn: "basic",
        cell: (info) => <Duration ms={buildDurations(info.row.original, now).wait} />,
      }),
      helper.accessor((row) => buildDurations(row, now).run ?? -1, {
        id: "run",
        header: () => t("run"),
        sortFn: "basic",
        cell: (info) => <Duration ms={buildDurations(info.row.original, now).run} />,
      }),
      helper.accessor((row) => row.nodes ?? -1, {
        id: "nodes",
        header: () => t("nodes"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.row.original.nodes ?? "-"}</span>,
      }),
      helper.accessor((row) => row.edges ?? -1, {
        id: "edges",
        header: () => t("edges"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.row.original.edges ?? "-"}</span>,
      }),
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
      helper.accessor((row) => row.error ?? "", {
        id: "error",
        header: () => t("error"),
        enableSorting: false,
        cell: (info) =>
          info.getValue() ? (
            <span className="flex max-w-xs items-start gap-1.5 text-xs whitespace-normal text-danger">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              <span className="line-clamp-2 [overflow-wrap:anywhere]" title={info.getValue()}>
                {info.getValue()}
              </span>
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          ),
      }),
    ]);
  }, [t, now]);
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
