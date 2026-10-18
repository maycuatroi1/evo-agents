import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AdminBrandingPage } from "@/components/admin/branding-page";
import { brandingLayerQuery, brandingVersionsQuery } from "@/components/admin/settings-data";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin.branding");
  return { title: t("title") };
}

/** The hub's branding and its versions, for hub admins; anyone else gets the API's 403 as the no-access state. */
export default async function BrandingPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, brandingLayerQuery(() => api)),
    prefetch(client, brandingVersionsQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminBrandingPage initialError={error} />
    </HydrationBoundary>
  );
}
