"use client";

import { Eye } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Segmented } from "@/components/data/segmented";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, NotFoundState, TableSkeleton } from "@/components/states/states";
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
import type { KgBuilds } from "@/lib/kg/types";

import { BuildHistory, BuildQueue, LatestBuild, NoBuilds, StatusTiles } from "./build-status";
import { KindSummary, NoGraph, SearchForm, SearchResults } from "./node-search";
import { useNow } from "./use-now";

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

function Status({ data }: { data: KgBuilds }) {
  const now = useNow(buildsActive(data));
  return <StatusTiles data={data} now={now} />;
}

/** /p/{project}/kg: the graph's status and builds, and node search. Read-only. */
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
        {(data) => <Status data={data} />}
      </QueryView>
      <SearchSection project={project} query={query} kind={kind} errors={errors} />
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

