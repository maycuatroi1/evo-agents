import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PM_DAILY, PM_PLUGIN, pmDailyQuery, validDate } from "@/components/pm/api";
import { DailyPage } from "@/components/pm/daily-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";
import { notFound } from "next/navigation";

type Props = { params: Promise<{ project: string }>; searchParams: Promise<{ date?: string | string[] }> };

async function projectOf(params: Props["params"]): Promise<string> {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  return project;
}

export async function generateMetadata({ params }: Pick<Props, "params">): Promise<Metadata> {
  const t = await getTranslations("pm.meta");
  return { title: t("daily", { project: await projectOf(params) }) };
}

/** The daily report of the project for the day `?date=` names, else today in its timezone. */
export default async function PmDailyRoute({ params, searchParams }: Props) {
  const project = await projectOf(params);
  const date = validDate([(await searchParams).date].flat()[0]);
  const plugin = await requirePlugin(PM_PLUGIN, project, PM_DAILY);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, pmDailyQuery(() => api, project, date));
  const title = plugin?.pages.find((item) => item.name === PM_DAILY)?.title ?? "PM daily";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <DailyPage project={project} title={title} initialError={date === null ? error : null} />
    </HydrationBoundary>
  );
}
