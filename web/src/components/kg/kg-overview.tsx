"use client";

import { Eye, ListChecks } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Segmented } from "@/components/data/segmented";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, NotFoundState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import {
  BUILD_HISTORY_ID,
  type BuildFilter,
  buildsActive,
  kgBuildsQuery,
  kgFailedBuildsQuery,
  kgGraphQuery,
  parseBuildFilter,
} from "@/lib/kg/queries";
import { reviewHref } from "@/lib/kg/routes";
import type { KgBuilds } from "@/lib/kg/types";

import { BuildHistory, BuildQueue, LatestBuild, NoBuilds } from "./build-status";
import { KindSummary, NoGraph, SearchForm, SearchResults } from "./node-search";
import { StatusTiles } from "./status-tiles";
import { useNow } from "./use-now";

// The graph's shape loads on demand too, with its own read (GET /v1/kg/{project}/shape), below the search.
const Shape = dynamic(() => import("./graph-shape").then((module) => module.GraphShapeSection));

// The cluster map loads on demand as well, with its own read (GET /v1/kg/{project}/clusters); cosmos.gl, which draws
// it, loads later still, only once a canvas mounts.
const ClusterMap = dynamic(() => import("./cluster-map").then((module) => module.ClusterMapSection));

// The Commits read card loads on demand (next/dynamic): imported statically, its code sat in a chunk that both pages
// of the graph, this one and the node page, load first, and put each over its budget in bundle-budget.json.
const SourceCommits = dynamic(() => import("./source-commits").then((module) => module.SourceCommits));

export type KgOverviewErrors = {
  builds: ApiErrorInfo | null;
  graph: ApiErrorInfo | null;
  search: ApiErrorInfo | null;
  /** Of the failed builds, prefetched when the URL asks for them (`?builds=failed`). */
  failed?: ApiErrorInfo | null;
};

function TilesSkeleton() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: 4 }, (_, i) => (
        <Skeleton key={i} className="h-24 w-full rounded-md" />
      ))}
    </div>
  );
}

function SearchSection({
  project,
  query,
  kind,
  errors,
}: {
  project: string;
  query: string;
  kind: string;
  errors: KgOverviewErrors;
}) {
  const t = useTranslations("kg.search");
  const graph = useHubQuery(kgGraphQuery(browserApi, project), errors.graph);
  const kinds = graph.status === "success" ? graph.data.kinds : [];
  return (
    <section aria-labelledby="kg-search-title">
    <Card data-testid="kg-search">
      <CardHeader>
        <CardTitle>
          <h2 id="kg-search-title">{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <SearchForm project={project} query={query} kind={kind} kinds={kinds} />
        {query ? (
          <SearchResults key={`${query}\u0000${kind}`} project={project} query={query} kind={kind} initialError={errors.search} />
        ) : (
          <QueryView state={graph} loading={<Skeleton className="h-16 w-full" />}>
            {(summary) => (summary.graph === null ? <NoGraph /> : <KindSummary kinds={summary.kinds} project={project} />)}
          </QueryView>
        )}
      </CardContent>
    </Card>
    </section>
  );
}

/**
 * The commit each git source of the graph read; the graph query is the one the search section reads. A graph whose
 * sources read no commit never fetches the card's chunk.
 */
function Commits({ project, error }: { project: string; error: ApiErrorInfo | null }) {
  const graph = useHubQuery(kgGraphQuery(browserApi, project), error);
  const sources = graph.status === "success" ? (graph.data.sources ?? []) : [];
  return sources.length > 0 ? <SourceCommits sources={sources} /> : null;
}

/** The history's filter in the URL (`?builds=failed`), changed through the History API like the lists' filters. */
function useBuildFilter(): [BuildFilter, (next: BuildFilter) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filter = parseBuildFilter(params.get("builds"));
  const set = (next: BuildFilter) => {
    const search = new URLSearchParams(params.toString());
    if (next === "failed") search.set("builds", "failed");
    else search.delete("builds");
    const text = search.toString();
    window.history.replaceState(null, "", `${pathname}${text ? `?${text}` : ""}`);
  };
  return [filter, set];
}

/** The project's latest failed builds, read on their own so a failure older than the latest builds still shows. */
function FailedHistory({ project, now, initialError }: { project: string; now: number | null; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("kg.history");
  const failed = useHubQuery(kgFailedBuildsQuery(browserApi, project), initialError);
  return (
    <QueryView state={failed} loading={<TableSkeleton rows={4} />}>
      {(data) => <BuildHistory builds={data.builds} now={now} caption={t("captionFailed")} empty={t("noFailed")} />}
    </QueryView>
  );
}

/** The build history under its head, with All and Failed; the admin overview links to Failed by its anchor. */
function History({ project, data, now, failedError }: { project: string; data: KgBuilds; now: number | null; failedError: ApiErrorInfo | null }) {
  const t = useTranslations("kg.history");
  const tBuilds = useTranslations("kg.builds");
  const [filter, setFilter] = useBuildFilter();
  return (
    <section aria-labelledby={BUILD_HISTORY_ID} className="flex flex-col gap-2" data-testid="kg-history" data-filter={filter}>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h3 id={BUILD_HISTORY_ID} className="scroll-mt-20 text-[15px] leading-[22px] font-semibold">
          {tBuilds("history")}
        </h3>
        <Segmented
          label={t("filter")}
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: t("all"), testId: "history-all" },
            { value: "failed", label: t("failed"), testId: "history-failed" },
          ]}
        />
      </div>
      {filter === "failed" ? (
        <FailedHistory project={project} now={now} initialError={failedError} />
      ) : (
        <BuildHistory builds={data.builds} now={now} />
      )}
    </section>
  );
}

function Builds({ project, data, failedError }: { project: string; data: KgBuilds; failedError: ApiErrorInfo | null }) {
  const t = useTranslations("kg.builds");
  const now = useNow(buildsActive(data));
  const [latest] = data.builds;
  return (
    <section aria-labelledby="kg-builds-title" className="flex flex-col gap-4" data-testid="kg-builds">
      <div className="flex flex-col gap-1">
        <h2 id="kg-builds-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="max-w-3xl text-sm text-pretty text-muted-foreground">
          {t.rich("description", { code: (chunks) => <code className="font-mono text-xs">{chunks}</code> })}
        </p>
      </div>
      {latest ? (
        <>
          <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:items-start">
            <LatestBuild build={latest} now={now} />
            <BuildQueue data={data} now={now} />
          </div>
          <History project={project} data={data} now={now} failedError={failedError} />
        </>
      ) : (
        <>
          <NoBuilds />
          {data.jobs.length > 0 ? <BuildQueue data={data} now={now} /> : null}
        </>
      )}
    </section>
  );
}

function Status({ project, data, graphError }: { project: string; data: KgBuilds; graphError: ApiErrorInfo | null }) {
  const now = useNow(buildsActive(data));
  const graph = useHubQuery(kgGraphQuery(browserApi, project), graphError);
  return <StatusTiles data={data} now={now} visible={graph.status === "success" ? graph.data.graph : null} />;
}

/**
 * /p/{project}/kg: the graph's status, the commits its sources read, node search, the cluster map, the graph's shape and
 * the builds. Read-only.
 */
export function KgOverview({
  project,
  query,
  kind,
  errors,
}: {
  project: string;
  query: string;
  kind: string;
  errors: KgOverviewErrors;
}) {
  const t = useTranslations("kg");
  const tStates = useTranslations("states");
  // Builds in progress are the page's live work: the top bar follows their query while it polls.
  const builds = useHubQuery(kgBuildsQuery(browserApi, project), errors.builds, { live: true });
  if (builds.status === "error" && builds.error.status === 404) {
    return (
      <NotFoundState
        title={tStates("projectNotFoundTitle", { name: project })}
        description={tStates("projectNotFoundDescription")}
      />
    );
  }
  if (builds.status === "error" && builds.error.status === 403) return <ApiErrorState error={builds.error} />;
  const header = (
    <PageHeader
      title={t("title")}
      tags={
        <Tag data-testid="kg-read-only">
          <Eye aria-hidden="true" />
          {t("readOnly")}
        </Tag>
      }
      actions={
        <Button asChild variant="outline" size="sm">
          <Link href={reviewHref(project)} data-testid="kg-review-link">
            <ListChecks aria-hidden="true" />
            {t("review.open")}
          </Link>
        </Button>
      }
    />
  );
  if (builds.status === "error") {
    return (
      <>
        {header}
        <ApiErrorState error={builds.error} onRetry={builds.retry} />
      </>
    );
  }
  return (
    <>
      {header}
      <QueryView state={builds} loading={<TilesSkeleton />}>
        {(data) => <Status project={project} data={data} graphError={errors.graph} />}
      </QueryView>
      <Commits project={project} error={errors.graph} />
      <SearchSection project={project} query={query} kind={kind} errors={errors} />
      <ClusterMap project={project} />
      <Shape project={project} />
      <QueryView state={builds} loading={<TableSkeleton rows={4} />}>
        {(data) => <Builds project={project} data={data} failedError={errors.failed ?? null} />}
      </QueryView>
    </>
  );
}

/** The skeleton of the whole page while the route loads. */
export function KgOverviewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-7 w-64" />
      </div>
      <TilesSkeleton />
      <Skeleton className="h-40 w-full rounded-md" />
      <TableSkeleton rows={4} />
    </div>
  );
}

