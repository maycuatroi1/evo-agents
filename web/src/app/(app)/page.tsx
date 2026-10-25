import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { HomePage } from "@/components/home/home-page";
import { workersQuery } from "@/components/workers/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { todayQuery } from "@/lib/notification-queries";
import { overviewQuery } from "@/lib/queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("home");
  return { title: t("title") };
}

/**
 * Home: the signed-in member's overview over every project of their grants (GET /v1/me/overview), their workers for
 * the Fleet card and Today (GET /v1/me/today), read on the server so the page renders complete, then kept current in
 * the browser. Today is read once a visit, here. A failed workers list or Today is not the page's failure: the Fleet
 * card asks again on its own, and Today says so inside its card.
 */
export default async function HomeRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error, , todayError] = await Promise.all([
    prefetch(client, overviewQuery(() => api)),
    prefetch(client, workersQuery(() => api)),
    prefetch(client, todayQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <HomePage initialError={error} todayError={todayError} />
    </HydrationBoundary>
  );
}
