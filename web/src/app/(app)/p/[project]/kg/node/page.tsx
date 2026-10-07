import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgNodePage } from "@/components/kg/kg-node-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { kgNeighbourhoodQuery, kgNodeQuery, parseHops } from "@/lib/kg/queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

const MAX_ID = 1000;

function nodeId(value: string | string[] | undefined): string {
  const text = Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
  return text.length <= MAX_ID ? text : "";
}

export async function generateMetadata({ searchParams }: Props): Promise<Metadata> {
  const t = await getTranslations("kg");
  const id = nodeId((await searchParams).id);
  return { title: id ? `${id}: ${t("title")}` : t("title") };
}

/** /p/{project}/kg/node?id=&hops=: the node and its neighbourhood, prefetched with the visitor's cookie. */
export default async function KgNodeRoute({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const search = await searchParams;
  const id = nodeId(search.id);
  const hops = parseHops(search.hops);
  const client = getQueryClient();
  let errors = { node: null, neighbourhood: null } as {
    node: Awaited<ReturnType<typeof prefetch>>;
    neighbourhood: Awaited<ReturnType<typeof prefetch>>;
  };
  if (id) {
    const api = await serverApi();
    const [node, neighbourhood] = await Promise.all([
      prefetch(client, kgNodeQuery(() => api, project, id)),
      prefetch(client, kgNeighbourhoodQuery(() => api, project, id, hops)),
    ]);
    errors = { node, neighbourhood };
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgNodePage project={project} id={id} hops={hops} errors={errors} />
    </HydrationBoundary>
  );
}
