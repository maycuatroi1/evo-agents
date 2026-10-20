import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { curatorStatusQuery, nightsQuery } from "@/components/curator/queries";
import { readRuntimeRange } from "@/components/curator/runtime-model";
import { RuntimePage } from "@/components/curator/runtime-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("curator");
  return { title: t("metaRuntime", { project: decodeURIComponent((await params).project) }) };
}

/**
 * The runtime and the harness of a project over the nights the URL names: the night shift's head and its nights, read
 * on the server; each counted night's figures load in the browser.
 */
export default async function ProjectRuntimePage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const nights = (await searchParams).nights;
  const range = readRuntimeRange(Array.isArray(nights) ? (nights[0] ?? null) : (nights ?? null));
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, curatorStatusQuery(() => api, project));
  if (error === null) await prefetch(client, nightsQuery(() => api, project, range));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <RuntimePage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
