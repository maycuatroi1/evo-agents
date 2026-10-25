import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MarketPage } from "@/components/market/market-page";
import { marketQuery } from "@/components/market/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("market");
  return { title: t("title") };
}

/** The plugins of every market the hub reads, which every signed-in member browses. */
export default async function MarketRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, marketQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MarketPage initialError={error} />
    </HydrationBoundary>
  );
}
