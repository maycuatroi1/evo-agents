import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { blobStoreQuery, SKILL_NAME, skillQuery } from "@/components/skills/queries";
import { SkillDetail } from "@/components/skills/skill-detail";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = { params: Promise<{ project: string; name: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: decodeURIComponent((await params).name) };
}

/** A skill of a project: every version, its source, and its bundle to download. */
export default async function ProjectSkillPage({ params }: Props) {
  const { project: rawProject, name: rawName } = await params;
  const project = decodeURIComponent(rawProject);
  const name = decodeURIComponent(rawName);
  if (!PROJECT_NAME.test(project) || !SKILL_NAME.test(name)) {
    const t = await getTranslations("skills.detail");
    return <NotFoundState title={t("notFoundTitle", { name })} description={t("notFoundDescription")} />;
  }
  const place = { kind: "project", project } as const;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, skillQuery(() => api, place, name)),
    prefetch(client, projectQuery(() => api, project)),
    prefetch(client, blobStoreQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SkillDetail place={place} name={name} initialError={error} />
    </HydrationBoundary>
  );
}
