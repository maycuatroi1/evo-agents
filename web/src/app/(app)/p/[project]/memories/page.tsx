import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MemoryBrowser } from "@/components/memories/memory-browser";
import { readFilters, recordParams, type SearchRecord } from "@/components/memories/memory-filters";
import { memoryListQuery, memorySearchQuery } from "@/components/memories/queries";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<SearchRecord>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("memories");
  return { title: `${t("projectTitle")}: ${decodeURIComponent((await params).project)}` };
}

/** The memories of a project the visitor holds a grant on, filtered by the API to what the grant reaches. */
export default async function ProjectMemoriesPage({ params, searchParams }: Props) {
  const name = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(name)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name })} description={t("projectNotFoundDescription")} />;
  }
  const filters = readFilters(recordParams(await searchParams));
  const scope = { kind: "project", project: name } as const;
  const api = await serverApi();
  const client = getQueryClient();
  const [projectError, error] = await Promise.all([
    prefetch(client, projectQuery(() => api, name)),
    filters.q
      ? prefetch(client, memorySearchQuery(() => api, scope, filters.q, filters.location))
      : prefetch(client, memoryListQuery(() => api, scope)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MemoryBrowser scope={scope} initialError={error} projectError={projectError} />
    </HydrationBoundary>
  );
}
