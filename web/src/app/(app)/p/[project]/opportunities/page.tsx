import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { OpportunitiesPage } from "@/components/loop/opportunities-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { opportunitiesQuery } from "@/lib/opportunity-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("opportunities.list");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/** A project's opportunities, read on the server. */
export default async function ProjectOpportunitiesPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, opportunitiesQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <OpportunitiesPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
