import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { DecidePage } from "@/components/loop/decide-page";
import { SlotPanels } from "@/components/plugins/slot-panels";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { decisionBoardQuery } from "@/lib/loop-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("loop.decide");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/**
 * What waits for a person in a project, read on the server so the page renders its gates complete; above the board,
 * the panels of the slot "decide": each enabled plugin's page of that slot, framed (marketing's growth decisions).
 */
export default async function ProjectDecidePage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, decisionBoardQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <DecidePage project={project} initialError={error} extra={<SlotPanels project={project} slot="decide" />} />
    </HydrationBoundary>
  );
}
