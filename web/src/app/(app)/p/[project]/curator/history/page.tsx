import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { HistoryPage } from "@/components/curator/history-page";
import { readHistoryFilters, sessionsQueryOf } from "@/components/curator/history-model";
import { clustersQuery, figuresQuery, historyStatusQuery, sessionsQuery, transcriptUsageQuery } from "@/components/curator/insight-queries";
import { curatorStatusQuery } from "@/components/curator/queries";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("curator");
  return { title: t("metaHistory", { project: decodeURIComponent((await params).project) }) };
}

/** Every value of the page's search parameters, a repeated one (`source`) included. */
function searchOf(record: Record<string, string | string[] | undefined>): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(record)) {
    for (const each of Array.isArray(value) ? value : value === undefined ? [] : [value]) params.append(key, each);
  }
  return params;
}

/**
 * A project's history as the Curator reads it: the night shift's head, how far the hub holds what the machines
 * reported, the history run, the clusters and the sessions the URL's filters name, read on the server so the page
 * renders complete; a search runs in the browser.
 */
export default async function ProjectHistoryPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const filters = readHistoryFilters(searchOf(await searchParams));
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, curatorStatusQuery(() => api, project));
  if (error === null) {
    await Promise.all([
      prefetch(client, historyStatusQuery(() => api, project)),
      prefetch(client, transcriptUsageQuery(() => api, project)),
      prefetch(client, figuresQuery(() => api, project)),
      prefetch(client, clustersQuery(() => api, project)),
      prefetch(client, sessionsQuery(() => api, project, sessionsQueryOf(filters))),
    ]);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <HistoryPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
