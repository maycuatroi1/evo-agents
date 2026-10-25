import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotFoundState } from "@/components/states/states";
import { PoolDetail } from "@/components/workers/pool-detail";
import { POOL_NAME } from "@/components/workers/pool-model";
import { poolQuery } from "@/components/workers/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { params: Promise<{ name: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const name = decodeURIComponent((await params).name);
  const t = await getTranslations("workers.pools.detail");
  return { title: POOL_NAME.test(name) ? name : t("title") };
}

/** One of the visitor's pools; another member's pool is not found, the same as a name no pool has. */
export default async function PoolRoute({ params }: Props) {
  const name = decodeURIComponent((await params).name);
  const t = await getTranslations("workers.pools.detail");
  if (!POOL_NAME.test(name)) return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, poolQuery(() => api, name));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PoolDetail name={name} initialError={error} />
    </HydrationBoundary>
  );
}
