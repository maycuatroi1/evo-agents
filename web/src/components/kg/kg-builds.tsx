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

import { BuildHistory, latestSucceeded, NoBuilds } from "./build-status";
import { KgHeader } from "./kg-header";
import { OptimizeButton } from "./optimize-button";
import { useNow } from "./use-now";

// The graph's shape loads on demand, with its own read (GET /v1/kg/{project}/shape).
const Shape = dynamic(() => import("./graph-shape").then((module) => module.GraphShapeSection));

// The banner, the figures and the Graph in use card load on demand too, rendered on the server all the same:
// imported statically, the MetricStrip, the banner and the card's facts put the route over its budget in
// bundle-budget.json.
const BuildActivity = dynamic(() => import("./build-activity").then((module) => module.BuildActivity));
const BuildFigures = dynamic(() => import("./build-figures").then((module) => module.BuildFigures));
const GraphInUse = dynamic(() => import("./graph-in-use").then((module) => module.GraphInUse));

// The Commits read card loads on demand (next/dynamic): imported statically, its code sat in a chunk that both pages
// of the graph, this one and the node page, load first, and put each over its budget in bundle-budget.json.
const SourceCommits = dynamic(() => import("./source-commits").then((module) => module.SourceCommits));

export type KgBuildsErrors = {
  builds: ApiErrorInfo | null;
  graph: ApiErrorInfo | null;
  /** Of the failed builds, prefetched when the URL asks for them (`?builds=failed`). */
  failed?: ApiErrorInfo | null;
};

function OverviewSkeleton() {
  return (
    <>
      <Skeleton className="h-[78px] w-full rounded-md" />
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-48 w-full rounded-md" />
        <Skeleton className="h-48 w-full rounded-md" />
      </div>
    </>
  );
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

/**
 * The build history under its head, with All and Failed; the admin overview links to Failed by its anchor. A build is
 * queued by a push, never from the web, and the line under the head says so.
 */
function History({ project, data, failedError }: { project: string; data: KgBuilds; failedError: ApiErrorInfo | null }) {
  const t = useTranslations("kg.history");
  const tBuilds = useTranslations("kg.builds");
  const now = useNow(buildsActive(data));
  const [filter, setFilter] = useBuildFilter();
  return (
    <section aria-labelledby={BUILD_HISTORY_ID} className="flex flex-col gap-3" data-testid="kg-history" data-filter={filter}>
      <div className="flex flex-wrap items-end justify-between gap-x-3 gap-y-2">
        <h2 id={BUILD_HISTORY_ID} className="min-w-0 scroll-mt-20 text-[15px] leading-[22px] font-semibold">
          {tBuilds("history")}
        </h2>
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

/**
 * The top of the page: a banner only while a build runs or waits or when the newest one failed, the graph and its
 * builds in figures, then the graph the pages read beside the commits its sources read.
 */
function Overview({ project, data, graphError }: { project: string; data: KgBuilds; graphError: ApiErrorInfo | null }) {
  const now = useNow(buildsActive(data));
  const graph = useHubQuery(kgGraphQuery(browserApi, project), graphError);
  const sources = graph.status === "success" ? (graph.data.sources ?? []) : [];
  return (
    <>
      <BuildActivity builds={data.builds} now={now} />
      <BuildFigures builds={data.builds} visible={graph.status === "success" ? graph.data.graph : null} />
      <div className="grid gap-4 lg:grid-cols-2">
        <GraphInUse graph={latestSucceeded(data.builds)} className={sources.length > 0 ? undefined : "lg:col-span-2"} />
        {sources.length > 0 ? <SourceCommits sources={sources} /> : null}
      </div>
    </>
  );
}

/**
 * /p/{project}/kg/builds: what the builds are doing when it needs a word, the graph in figures, the graph in use and
 * the commits its sources read, the build history, then the graph's shape, under the knowledge graph's tabs. Read-only.
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
  const header = <KgHeader project={project} current="builds" actions={<OptimizeButton project={project} />} />;
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
      <QueryView state={builds} loading={<OverviewSkeleton />}>
        {(data) =>
          data.builds.length === 0 ? (
            <NoBuilds />
          ) : (
            <>
              <Overview project={project} data={data} graphError={errors.graph} />
              <History project={project} data={data} failedError={errors.failed ?? null} />
              {/* No graph, no shape: the section waits for the first build that succeeds. */}
              {latestSucceeded(data.builds) ? <Shape project={project} /> : null}
            </>
          )
        }
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
      <OverviewSkeleton />
      <TableSkeleton rows={4} />
    </div>
  );
}
