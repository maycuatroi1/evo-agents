import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { sprintsQuery, tasksQuery, tasksSettingsQuery } from "@/components/tasks/api";
import { BacklogPage } from "@/components/tasks/backlog-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { TASKS_PLUGIN } from "@/lib/core-plugin-links";
import { requirePlugin } from "@/lib/plugins-server";

import { pageTitle, projectParam } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("tasks.meta");
  return { title: t("backlog", { project: await projectParam(params) }) };
}

/** Every task of the project by sprint. */
export default async function TasksBacklogRoute({ params }: Props) {
  const project = await projectParam(params);
  const plugin = await requirePlugin(TASKS_PLUGIN, project, "backlog");
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, tasksSettingsQuery(() => api, project)),
    prefetch(client, sprintsQuery(() => api, project)),
    prefetch(client, tasksQuery(() => api, project, { kind: "all" })),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <BacklogPage project={project} title={pageTitle(plugin, "backlog", "Backlog")} initialError={error} />
    </HydrationBoundary>
  );
}
