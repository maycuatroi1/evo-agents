import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { workersQuery } from "@/components/workers/queries";
import { WorkersPage } from "@/components/workers/workers-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("workers");
  return { title: t("title") };
}

/** The visitor's workers (every worker for a hub admin), refreshed in the browser every 10 seconds. */
export default async function WorkersRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, workersQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <WorkersPage initialError={error} />
    </HydrationBoundary>
  );
}
