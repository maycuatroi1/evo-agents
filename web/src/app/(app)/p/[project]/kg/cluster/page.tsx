import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgClusterPage } from "@/components/kg/kg-cluster-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { kgClusterQuery } from "@/lib/kg/queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

const MAX_ID = 200;

function clusterId(value: string | string[] | undefined): string {
  const text = Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
  return text.length <= MAX_ID ? text : "";
}

export async function generateMetadata({ searchParams }: Props): Promise<Metadata> {
  const t = await getTranslations("kg.cluster");
  const id = clusterId((await searchParams).id);
  return { title: id ? `${id}: ${t("title")}` : t("title") };
}

/** /p/{project}/kg/cluster?id=: one cluster, prefetched with the visitor's cookie. */
export default async function KgClusterRoute({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const id = clusterId((await searchParams).id);
  const client = getQueryClient();
  let error: Awaited<ReturnType<typeof prefetch>> = null;
  if (id) {
    const api = await serverApi();
    error = await prefetch(client, kgClusterQuery(() => api, project, id));
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgClusterPage project={project} id={id} error={error} />
    </HydrationBoundary>
  );
}
