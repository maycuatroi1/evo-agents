import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MergeQueuePage } from "@/components/merge-queue/queue-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { mergeQueueQuery } from "@/lib/merge-queue";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("mergeQueue");
  return { title: t("metaQueue", { project: decodeURIComponent((await params).project) }) };
}

/** A project's merge queue on GitLab, read on the server so the page renders its merge requests complete. */
export default async function ProjectMergeQueuePage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, mergeQueueQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MergeQueuePage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
