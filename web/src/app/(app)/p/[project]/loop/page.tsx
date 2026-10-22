import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { LoopPage } from "@/components/loop/loop-page";
import { itemsFilterOf, readLoopFilters } from "@/components/loop/model";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { goalsQuery, loopItemsQuery, loopQuery } from "@/lib/loop-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("loop.page");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/** A project's Loop and the work items its address filters (`?state=&kind=&goal=&view=chain`), read on the server. */
export default async function ProjectLoopPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const search = await searchParams;
  const filters = readLoopFilters({ get: (name) => (typeof search[name] === "string" ? (search[name] as string) : null) });
  const api = await serverApi();
  const client = getQueryClient();
  const [error, itemsError] = await Promise.all([
    prefetch(client, loopQuery(() => api, project)),
    prefetch(client, loopItemsQuery(() => api, project, itemsFilterOf(filters))),
    prefetch(client, goalsQuery(() => api, project)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <LoopPage project={project} initialError={error} itemsError={error ? null : itemsError} />
    </HydrationBoundary>
  );
}
