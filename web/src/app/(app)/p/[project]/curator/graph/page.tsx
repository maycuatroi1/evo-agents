import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { GraphHealthPage } from "@/components/curator/graph-health-page";
import { figuresQuery, kgHealthQuery } from "@/components/curator/insight-queries";
import { curatorStatusQuery } from "@/components/curator/queries";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("curator");
  return { title: t("metaGraph", { project: decodeURIComponent((await params).project) }) };
}

/** The health of a project's knowledge graph over time and the night's figures that hold the worker's sync, read on the server. */
export default async function ProjectGraphHealthPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, curatorStatusQuery(() => api, project));
  if (error === null) {
    await Promise.all([prefetch(client, kgHealthQuery(() => api, project)), prefetch(client, figuresQuery(() => api, project))]);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <GraphHealthPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
