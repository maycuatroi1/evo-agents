import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { tasksQuery, tasksSettingsQuery } from "@/components/tasks/api";
import { MinePage } from "@/components/tasks/mine-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { TASKS_PLUGIN } from "@/lib/core-plugin-links";
import { requirePlugin } from "@/lib/plugins-server";

import { pageTitle, projectParam } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("tasks.meta");
  return { title: t("mine", { project: await projectParam(params) }) };
}

/** The visitor's own tasks in the project. */
export default async function TasksMineRoute({ params }: Props) {
  const project = await projectParam(params);
  const plugin = await requirePlugin(TASKS_PLUGIN, project, "mine");
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, tasksSettingsQuery(() => api, project)),
    prefetch(client, tasksQuery(() => api, project, { kind: "mine" })),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MinePage project={project} title={pageTitle(plugin, "mine", "My work")} initialError={error} />
    </HydrationBoundary>
  );
}
