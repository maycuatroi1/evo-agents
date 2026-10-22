import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { OrgPage } from "@/components/org/org-page";
import { orgQuery } from "@/components/org/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("org");
  return { title: t("title") };
}

/** The org chart: units, seats, ceilings and effective levels, for every member signed in. */
export default async function OrgRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, orgQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <OrgPage initialError={error} />
    </HydrationBoundary>
  );
}
