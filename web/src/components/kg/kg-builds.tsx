"use client";

import dynamic from "next/dynamic";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { Segmented } from "@/components/data/segmented";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, NotFoundState, TableSkeleton } from "@/components/states/states";
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
import type { KgBuilds } from "@/lib/kg/types";

import { BuildHistory, BuildQueue, LatestBuild, NoBuilds } from "./build-status";
import { KgHeader } from "./kg-header";
import { StatusTiles } from "./status-tiles";
import { useNow } from "./use-now";

// The graph's shape loads on demand, with its own read (GET /v1/kg/{project}/shape).
const Shape = dynamic(() => import("./graph-shape").then((module) => module.GraphShapeSection));

// The Commits read card loads on demand (next/dynamic): imported statically, its code sat in a chunk that both pages
// of the graph, this one and the node page, load first, and put each over its budget in bundle-budget.json.
const SourceCommits = dynamic(() => import("./source-commits").then((module) => module.SourceCommits));

export type KgBuildsErrors = {
  builds: ApiErrorInfo | null;
  graph: ApiErrorInfo | null;
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

/**
 * The commit each git source of the graph read, from the graph summary the status tiles read too. A graph whose
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
 * /p/{project}/kg/builds: the graph's status, the commits its sources read, the graph's shape and the builds, under the
 * knowledge graph's tabs. Read-only.
 */
export function KgBuildsPage({ project, errors }: { project: string; errors: KgBuildsErrors }) {
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
  const header = <KgHeader project={project} current="builds" />;
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
      <Shape project={project} />
      <QueryView state={builds} loading={<TableSkeleton rows={4} />}>
        {(data) => <Builds project={project} data={data} failedError={errors.failed ?? null} />}
      </QueryView>
    </>
  );
}

/** The skeleton of the builds page while the route loads. */
export function KgBuildsSkeleton() {
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

