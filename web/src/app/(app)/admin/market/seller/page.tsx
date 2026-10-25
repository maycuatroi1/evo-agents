import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { sellerInfoQuery, sellerListingsQuery, sellerOrdersQuery } from "@/components/market/queries";
import { SellerPage } from "@/components/market/seller-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("market.seller");
  return { title: t("title") };
}

/** The seller's orders, revenue, key and listings, for hub admins of a hub with the feature seller on. */
export default async function SellerRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, sellerInfoQuery(() => api)),
    prefetch(client, sellerListingsQuery(() => api)),
    prefetch(client, sellerOrdersQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SellerPage initialError={error} />
    </HydrationBoundary>
  );
}
