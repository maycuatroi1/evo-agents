import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ProjectPluginsPage } from "@/components/plugins/project-plugins-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { projectPluginsQuery } from "@/lib/plugin-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plugins.project");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/** A project's plugins, for every member of it; anyone else gets the no-access state (the API answers 403 or 404). */
export default async function ProjectPluginsRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) {
    const t = await getTranslations("states");
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, projectPluginsQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProjectPluginsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
