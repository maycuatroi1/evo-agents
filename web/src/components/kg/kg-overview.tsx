"use client";

import { Eye } from "lucide-react";
import { useTranslations } from "next-intl";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, NotFoundState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { buildsActive, kgBuildsQuery, kgGraphQuery } from "@/lib/kg/queries";
import type { KgBuilds } from "@/lib/kg/types";

import { BuildHistory, BuildQueue, LatestBuild, NoBuilds, StatusTiles } from "./build-status";
import { KindSummary, NoGraph, SearchForm, SearchResults } from "./node-search";
import { useNow } from "./use-now";

export type KgOverviewErrors = {
  builds: ApiErrorInfo | null;
  graph: ApiErrorInfo | null;
  search: ApiErrorInfo | null;
};

function TilesSkeleton() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {Array.from({ length: 4 }, (_, i) => (
        <Skeleton key={i} className="h-24 w-full rounded-xl" />
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

function Builds({ data }: { data: KgBuilds }) {
  const t = useTranslations("kg.builds");
  const now = useNow(buildsActive(data));
  const [latest] = data.builds;
  return (
    <section aria-labelledby="kg-builds-title" className="flex flex-col gap-4" data-testid="kg-builds">
      <div className="flex flex-col gap-1">
        <h2 id="kg-builds-title" className="text-lg font-semibold tracking-tight">
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
          <div className="flex flex-col gap-2">
            <h3 className="text-base font-medium">{t("history")}</h3>
            <BuildHistory builds={data.builds} now={now} />
          </div>
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
  const builds = useHubQuery(kgBuildsQuery(browserApi, project), errors.builds);
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
      eyebrow={<span className="font-mono normal-case">{project}</span>}
      title={t("title")}
      description={t("description")}
      meta={
        <Badge variant="outline" data-testid="kg-read-only">
          <Eye aria-hidden="true" />
          {t("readOnly")}
        </Badge>
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
        {(data) => <Builds data={data} />}
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
      <Skeleton className="h-40 w-full rounded-xl" />
      <TableSkeleton rows={4} />
    </div>
  );
}

