import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { baselineQuery } from "@/components/canvas/api";
import { BaselinePage } from "@/components/canvas/baseline-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { baselineNumber, canvasParams } from "../../../../route-params";

type Props = { params: Promise<{ project: string; slug: string; number: string }> };

async function names(params: Props["params"]) {
  return { ...(await canvasParams(params)), number: baselineNumber((await params).number) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("canvas.meta");
  return { title: t("baseline", await names(params)) };
}

/** A baseline's review, the page the notice canvas_baseline links: its items and the decisions on them. */
export default async function BaselineRoute({ params }: Props) {
  const { project, slug, number } = await names(params);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, baselineQuery(() => api, project, slug, number));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <BaselinePage project={project} slug={slug} number={number} initialError={error} />
    </HydrationBoundary>
  );
}
