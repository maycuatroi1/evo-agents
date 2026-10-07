import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { skillsQuery } from "@/components/skills/queries";
import { SkillsBrowser } from "@/components/skills/skills-browser";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("skills");
  return { title: `${t("projectTitle")}: ${decodeURIComponent((await params).project)}` };
}

/** The skills of a project, for its members; anyone else gets the no-access state (the API answers 403). */
export default async function ProjectSkillsPage({ params }: Props) {
  const name = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(name)) {
    const t = await getTranslations("states");
    return <NotFoundState title={t("projectNotFoundTitle", { name })} description={t("projectNotFoundDescription")} />;
  }
  const place = { kind: "project", project: name } as const;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, skillsQuery(() => api, place)),
    prefetch(client, projectQuery(() => api, name)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SkillsBrowser place={place} initialError={error} />
    </HydrationBoundary>
  );
}
