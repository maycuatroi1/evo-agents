import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { MARKETING_WEEKLY, MARKETING_PLUGIN, marketingWeeklyQuery } from "@/components/marketing/api";
import { WeeklyPage } from "@/components/marketing/weekly-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

async function projectOf(params: Props["params"]): Promise<string> {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  return project;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("marketing.meta");
  return { title: t("weekly", { project: await projectOf(params) }) };
}

/** The Growth section of the weekly bulletin, the link of the Monday notice. */
export default async function MarketingWeeklyRoute({ params }: Props) {
  const project = await projectOf(params);
  const plugin = await requirePlugin(MARKETING_PLUGIN, project, MARKETING_WEEKLY);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, marketingWeeklyQuery(() => api, project));
  const title = plugin?.pages.find((item) => item.name === MARKETING_WEEKLY)?.title ?? "Growth weekly";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <WeeklyPage project={project} title={title} initialError={error} />
    </HydrationBoundary>
  );
}
