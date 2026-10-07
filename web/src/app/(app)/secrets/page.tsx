import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { secretsQuery } from "@/components/secrets/queries";
import { SecretsPage } from "@/components/secrets/secrets-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("secrets");
  return { title: t("title") };
}

/** The visitor's own secrets, a hub admin's too: names, kinds, targets, bindings and dates, never a value. */
export default async function SecretsRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, secretsQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SecretsPage initialError={error} />
    </HydrationBoundary>
  );
}
