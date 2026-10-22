import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ChainPage } from "@/components/loop/chain-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { loopItemQuery } from "@/lib/loop-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; id: string }> };

async function names(params: Props["params"]) {
  const { project, id } = await params;
  return { project: decodeURIComponent(project), id: decodeURIComponent(id) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("loop.chain");
  const { project, id } = await names(params);
  return { title: t("meta", { project, id }) };
}

/** One work item of a project's Loop with its chain, read on the server. */
export default async function LoopItemPage({ params }: Props) {
  const { project, id } = await names(params);
  const t = await getTranslations("states");
  const chain = await getTranslations("loop.chain");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const number = /^[1-9][0-9]{0,17}$/.test(id) ? Number(id) : null;
  if (number === null || !Number.isSafeInteger(number)) {
    return <NotFoundState title={chain("notFoundTitle", { id })} description={chain("notFoundText")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, loopItemQuery(() => api, project, number));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ChainPage project={project} id={number} initialError={error} />
    </HydrationBoundary>
  );
}
