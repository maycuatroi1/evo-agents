import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotFoundState } from "@/components/states/states";
import { parseWorkerId, workerQuery } from "@/components/workers/queries";
import { WorkerDetail } from "@/components/workers/worker-detail";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { params: Promise<{ id: string }> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("workers.detail");
  return { title: t("title") };
}

/** One worker; another member's worker is not found, the same as an id the hub never gave out. */
export default async function WorkerRoute({ params }: Props) {
  const id = parseWorkerId((await params).id);
  const t = await getTranslations("workers.detail");
  if (id === null) return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, workerQuery(() => api, id));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <WorkerDetail id={id} initialError={error} />
    </HydrationBoundary>
  );
}
