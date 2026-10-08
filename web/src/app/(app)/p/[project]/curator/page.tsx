import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { CuratorPage } from "@/components/curator/curator-page";
import { curatorStatusQuery, NIGHTS_SHOWN, nightsQuery } from "@/components/curator/queries";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("curator");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/**
 * A project's Curator, for any member with a grant on it: where its night shift stands and its nights, read on the
 * server so the page renders complete; the browser keeps them current.
 */
export default async function ProjectCuratorPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, curatorStatusQuery(() => api, project));
  if (error === null) await prefetch(client, nightsQuery(() => api, project, NIGHTS_SHOWN));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CuratorPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
