import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { sprintsQuery, tasksPeopleQuery, tasksQuery, tasksSettingsQuery } from "@/components/tasks/api";
import { BoardPage } from "@/components/tasks/board-page";
import { type BoardAsk, boardList } from "@/components/tasks/model";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { TASKS_BOARD, TASKS_PLUGIN } from "@/lib/core-plugin-links";
import { requirePlugin } from "@/lib/plugins-server";

import { pageTitle, projectParam, searchText } from "../route-params";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("tasks.meta");
  return { title: t("board", { project: await projectParam(params) }) };
}

/**
 * The board of a project that enabled the core plugin tasks; the layout has checked it did. `sprint` names the sprint
 * it shows and `backlog=1` asks for the tasks of no sprint; without either it opens on the active sprint.
 */
export default async function TasksBoardRoute({ params, searchParams }: Props) {
  const project = await projectParam(params);
  const query = await searchParams;
  const sprint = searchText(query.sprint);
  const ask: BoardAsk = sprint ? { sprint } : searchText(query.backlog) === "1" ? { backlog: true } : null;
  const plugin = await requirePlugin(TASKS_PLUGIN, project, TASKS_BOARD);
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, tasksSettingsQuery(() => api, project)),
    prefetch(client, sprintsQuery(() => api, project)),
    prefetch(client, tasksPeopleQuery(() => api, project)),
  ]);
  const list = boardList(ask, client.getQueryData(sprintsQuery(() => api, project).queryKey));
  if (list) await prefetch(client, tasksQuery(() => api, project, list));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <BoardPage project={project} title={pageTitle(plugin, TASKS_BOARD, "Tasks")} ask={ask} initialError={error} />
    </HydrationBoundary>
  );
}
