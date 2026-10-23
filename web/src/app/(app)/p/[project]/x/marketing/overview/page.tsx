import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { MARKETING_OVERVIEW, MARKETING_PLUGIN, marketingStatusQuery } from "@/components/marketing/api";
import { OverviewPage } from "@/components/marketing/overview-page";
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
  return { title: t("overview", { project: await projectOf(params) }) };
}

/** The kill switch, the month's cost against its cap and the channels, read on the server so the page renders complete. */
export default async function MarketingOverviewRoute({ params }: Props) {
  const project = await projectOf(params);
  const plugin = await requirePlugin(MARKETING_PLUGIN, project, MARKETING_OVERVIEW);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, marketingStatusQuery(() => api, project));
  const title = plugin?.pages.find((item) => item.name === MARKETING_OVERVIEW)?.title ?? "Marketing";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <OverviewPage project={project} title={title} initialError={error} />
    </HydrationBoundary>
  );
}
