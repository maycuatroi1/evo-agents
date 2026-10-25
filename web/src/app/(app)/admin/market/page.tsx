import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AdminMarketPage } from "@/components/market/admin-market-page";
import { marketSourcesQuery, trustedPublishersQuery } from "@/components/market/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("market.admin");
  return { title: t("title") };
}

/** The markets the hub reads and the publisher signers its admins trusted, for hub admins; anyone else gets a 403. */
export default async function AdminMarketRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, marketSourcesQuery(() => api)),
    prefetch(client, trustedPublishersQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminMarketPage initialError={error} />
    </HydrationBoundary>
  );
}
