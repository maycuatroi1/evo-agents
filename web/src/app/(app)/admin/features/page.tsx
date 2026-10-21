import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AdminFeaturesPage } from "@/components/admin/features-page";
import { featureLayerQuery, featureVersionsQuery } from "@/components/admin/settings-data";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { featuresQuery } from "@/lib/hub-settings";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin.features");
  return { title: t("title") };
}

/**
 * Which features are on, for the hub and each project, for hub admins. The hub's own layer and its versions are read
 * here; the shell has read the features and the projects already. Anyone else gets the API's 403.
 */
export default async function FeaturesPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const [layerError] = await Promise.all([
    prefetch(client, featureLayerQuery(() => api, null)),
    prefetch(client, featureVersionsQuery(() => api, null)),
    prefetch(client, featuresQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminFeaturesPage initialError={layerError} />
    </HydrationBoundary>
  );
}
