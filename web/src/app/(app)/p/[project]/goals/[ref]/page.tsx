import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { GoalPage } from "@/components/loop/goal-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { goalQuery, loopGoalsQuery, loopItemsQuery } from "@/lib/loop-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; ref: string }> };

/** A goal's code (G-<number>) or the alias of a goal made from the charter, as `goals.REF` takes it. */
const GOAL_REF = /^(?:G-[1-9][0-9]{0,8}|[a-z0-9][a-z0-9-]{0,63})$/;

async function names(params: Props["params"]) {
  const { project, ref } = await params;
  return { project: decodeURIComponent(project), ref: decodeURIComponent(ref) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("loop.goal");
  const { project, ref } = await names(params);
  return { title: t("meta", { project, code: ref }) };
}

/** One goal of a project, with its metric measured now and its work items, read on the server. */
export default async function ProjectGoalPage({ params }: Props) {
  const { project, ref } = await names(params);
  const t = await getTranslations("states");
  const goal = await getTranslations("loop.goal");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  if (!GOAL_REF.test(ref)) return <NotFoundState title={goal("notFoundTitle", { code: ref })} description={goal("notFoundText")} />;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, goalQuery(() => api, project, ref)),
    prefetch(client, loopGoalsQuery(() => api, project)),
    prefetch(client, loopItemsQuery(() => api, project, { state: null, kind: null, goal: ref, open: false })),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <GoalPage project={project} code={ref} initialError={error} />
    </HydrationBoundary>
  );
}
