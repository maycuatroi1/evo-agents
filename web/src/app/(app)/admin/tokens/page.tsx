import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { adminUsersQuery, fromRecord, parseTokenFilters, tokenParams, tokensQuery } from "@/components/admin/data";
import { AdminTokens } from "@/components/admin/tokens-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin.tokens");
  return { title: t("title") };
}

export default async function TokensPage({ searchParams }: Props) {
  const filters = parseTokenFilters(fromRecord(await searchParams));
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, tokensQuery(() => api, tokenParams(filters))),
    prefetch(client, adminUsersQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminTokens initialError={error} />
    </HydrationBoundary>
  );
}
