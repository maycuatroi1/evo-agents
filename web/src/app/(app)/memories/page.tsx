import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MemoryBrowser } from "@/components/memories/memory-browser";
import { readFilters, recordParams, type SearchRecord } from "@/components/memories/memory-filters";
import { memoryListQuery, memorySearchQuery } from "@/components/memories/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { searchParams: Promise<SearchRecord> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("memories");
  return { title: t("personalTitle") };
}

/** The visitor's personal memories: directories outside every hub project, which only their owner sees. */
export default async function PersonalMemoriesPage({ searchParams }: Props) {
  const filters = readFilters(recordParams(await searchParams));
  const scope = { kind: "personal" } as const;
  const api = await serverApi();
  const client = getQueryClient();
  const error = filters.q
    ? await prefetch(client, memorySearchQuery(() => api, scope, filters.q, filters.location))
    : await prefetch(client, memoryListQuery(() => api, scope));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MemoryBrowser scope={scope} initialError={error} />
    </HydrationBoundary>
  );
}
