import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotFoundState } from "@/components/states/states";
import { agentsQuery } from "@/lib/agent-queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";
import { SLUG } from "@/lib/systems";
import { systemQuery } from "@/lib/systems-queries";

import { SystemRoute } from "./designer-slots";

type Props = { params: Promise<{ project: string; slug: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("systems");
  const { project, slug } = await params;
  return { title: t("systemMetaTitle", { slug: decodeURIComponent(slug), project: decodeURIComponent(project) }) };
}

/**
 * One agent system of a project: its designer, read by every member and changed by its admins. The system and the
 * project's agents (for the pins a node could move to) load with the page.
 */
export default async function ProjectSystemPage({ params }: Props) {
  const { project: rawProject, slug: rawSlug } = await params;
  const project = decodeURIComponent(rawProject);
  const slug = decodeURIComponent(rawSlug);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  if (!SLUG.test(slug)) {
    const s = await getTranslations("systems");
    return <NotFoundState title={s("notFoundTitle", { slug })} description={s("notFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([prefetch(client, systemQuery(() => api, project, slug)), prefetch(client, agentsQuery(() => api, project))]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SystemRoute project={project} slug={slug} initialError={error} />
    </HydrationBoundary>
  );
}
