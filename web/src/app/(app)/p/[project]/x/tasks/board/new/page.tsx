import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { sprintsQuery, tasksPeopleQuery, tasksSettingsQuery } from "@/components/tasks/api";
import { NewTaskPage } from "@/components/tasks/new-task-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { projectParam, searchText } from "../../route-params";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("tasks.meta");
  return { title: t("new", { project: await projectParam(params) }) };
}

/** The form of a new task; `link` fills in a link (an SRS item's code), `sprint` the sprint. */
export default async function NewTaskRoute({ params, searchParams }: Props) {
  const project = await projectParam(params);
  const query = await searchParams;
  const t = await getTranslations("tasks.newTask");
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, tasksSettingsQuery(() => api, project)),
    prefetch(client, sprintsQuery(() => api, project)),
    prefetch(client, tasksPeopleQuery(() => api, project)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <NewTaskPage project={project} title={t("heading")} link={searchText(query.link)} sprint={searchText(query.sprint)} initialError={error} />
    </HydrationBoundary>
  );
}
