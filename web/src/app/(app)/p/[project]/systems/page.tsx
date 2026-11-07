import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotFoundState } from "@/components/states/states";
import { SystemsPage } from "@/components/systems/systems-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";
import { systemsQuery } from "@/lib/systems-queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("systems");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/** A project's agent systems, for every member of it; anyone else gets the no-access state (the API answers 403). */
export default async function ProjectSystemsPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) {
    const t = await getTranslations("states");
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, systemsQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SystemsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
