import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { HomePage } from "@/components/home/home-page";
import { workersQuery } from "@/components/workers/queries";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { overviewQuery } from "@/lib/queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("home");
  return { title: t("title") };
}

/**
 * Home: the signed-in member's overview over every project of their grants (GET /v1/me/overview) and their workers for
 * the Fleet card, read on the server so the page renders complete, then kept current in the browser. A failed workers
 * list is not the page's failure: the Fleet card asks again on its own.
 */
export default async function HomeRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([prefetch(client, overviewQuery(() => api)), prefetch(client, workersQuery(() => api))]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <HomePage initialError={error} />
    </HydrationBoundary>
  );
}
