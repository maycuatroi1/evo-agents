import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AdminOverview } from "@/components/pages/admin-overview";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { adminStatsQuery } from "@/lib/queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin");
  return { title: t("title") };
}

/** Only hub admins get data here; the API answers 403 to anyone else, and the page says so. */
export default async function AdminPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, adminStatsQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminOverview initialError={error} />
    </HydrationBoundary>
  );
}
