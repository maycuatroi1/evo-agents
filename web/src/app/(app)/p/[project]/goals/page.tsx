import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { GoalsPage } from "@/components/loop/goals-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { goalsQuery, loopGoalsQuery } from "@/lib/loop-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("loop.goals");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/** A project's goals with their metric measured now, both read on the server. */
export default async function ProjectGoalsPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([prefetch(client, goalsQuery(() => api, project)), prefetch(client, loopGoalsQuery(() => api, project))]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <GoalsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
