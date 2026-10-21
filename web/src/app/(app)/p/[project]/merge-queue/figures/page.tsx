import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MergeFiguresPage } from "@/components/merge-queue/figures-page";
import { readDays } from "@/components/merge-queue/model";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { mergeFiguresQuery } from "@/lib/merge-queue";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("mergeQueue");
  return { title: t("metaFigures", { project: decodeURIComponent((await params).project) }) };
}

/** The merge queue's figures over the window the URL names (`?days=7`, 30 without it, or 90), read on the server. */
export default async function ProjectMergeFiguresPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const days = readDays((await searchParams).days);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, mergeFiguresQuery(() => api, project, days));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MergeFiguresPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
