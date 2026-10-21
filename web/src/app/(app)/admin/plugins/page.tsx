import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AdminPluginsPage } from "@/components/plugins/admin-plugins-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { pluginsQuery, publishersQuery } from "@/components/plugins/queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("plugins.admin");
  return { title: t("title") };
}

/** The hub's store of plugins and the publishers the operator trusts, for hub admins; anyone else gets the API's 403. */
export default async function PluginsPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, pluginsQuery(() => api)),
    prefetch(client, publishersQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminPluginsPage initialError={error} />
    </HydrationBoundary>
  );
}
