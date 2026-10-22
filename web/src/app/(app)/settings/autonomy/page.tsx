import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AutonomyPage } from "@/components/autonomy/autonomy-page";
import { settingsQuery } from "@/components/autonomy/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("autonomy.settings");
  return { title: t("title") };
}

/** The visitor's own autonomy: their level and preference, the ceiling over it, each class's mode and figures. */
export default async function AutonomyRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, settingsQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AutonomyPage initialError={error} />
    </HydrationBoundary>
  );
}
