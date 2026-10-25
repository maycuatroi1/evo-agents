import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PoolsPage } from "@/components/workers/pools-page";
import { poolsQuery } from "@/components/workers/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("workers.pools");
  return { title: t("title") };
}

/** The visitor's worker pools, with their nodes, refreshed in the browser every 10 seconds. */
export default async function PoolsRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, poolsQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PoolsPage initialError={error} />
    </HydrationBoundary>
  );
}
