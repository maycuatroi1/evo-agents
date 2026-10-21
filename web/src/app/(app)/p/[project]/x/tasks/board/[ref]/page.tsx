import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { taskQuery, tasksSettingsQuery } from "@/components/tasks/api";
import { TaskPage } from "@/components/tasks/task-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { taskParams } from "../../route-params";

type Props = { params: Promise<{ project: string; ref: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("tasks.meta");
  return { title: t("task", await taskParams(params)) };
}

/** A task whole, by its ref (`T-42`) or its number. */
export default async function TaskRoute({ params }: Props) {
  const { project, ref } = await taskParams(params);
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([prefetch(client, tasksSettingsQuery(() => api, project)), prefetch(client, taskQuery(() => api, project, ref))]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <TaskPage project={project} taskRef={ref} initialError={error} />
    </HydrationBoundary>
  );
}
