import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { adminOverviewQuery } from "@/components/admin/data";
import { AdminOverviewPage } from "@/components/admin/overview-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin");
  return { title: t("title") };
}

/** Only hub admins get data here; the API answers 403 to anyone else, and the page says so. */
export default async function AdminPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, adminOverviewQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminOverviewPage initialError={error} />
    </HydrationBoundary>
  );
}
