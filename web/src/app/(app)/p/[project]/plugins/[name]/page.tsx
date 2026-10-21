import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ProjectPluginPage } from "@/components/plugins/project-plugin-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PLUGIN_NAME, projectPluginQuery } from "@/lib/plugin-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; name: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { project, name } = await params;
  const t = await getTranslations("plugins.project");
  return { title: `${decodeURIComponent(name)} | ${t("metaTitle", { project: decodeURIComponent(project) })}` };
}

/** A plugin in a project: its permissions and pages, and the project's layer of its config. */
export default async function ProjectPluginRoute({ params }: Props) {
  const { project: rawProject, name: rawName } = await params;
  const project = decodeURIComponent(rawProject);
  const name = decodeURIComponent(rawName);
  if (!PROJECT_NAME.test(project) || !PLUGIN_NAME.test(name)) {
    const t = await getTranslations("plugins.page");
    return <NotFoundState title={t("missingTitle")} description={t("missingDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, projectPluginQuery(() => api, project, name));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProjectPluginPage project={project} name={name} initialError={error} />
    </HydrationBoundary>
  );
}
