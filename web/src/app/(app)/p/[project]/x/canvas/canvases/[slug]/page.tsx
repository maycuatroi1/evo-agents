import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { baselinesQuery, canvasQuery, proposalsQuery } from "@/components/canvas/api";
import { CanvasPage } from "@/components/canvas/canvas-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { canvasParams } from "../../route-params";

type Props = {
  params: Promise<{ project: string; slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("canvas.meta");
  return { title: t("canvas", await canvasParams(params)) };
}

/**
 * A canvas whole, with archived claims too when the address asks (`?archived=1`), its proposals and its baselines; all
 * read on the server, so the grid renders complete.
 */
export default async function CanvasRoute({ params, searchParams }: Props) {
  const { project, slug } = await canvasParams(params);
  const archived = (await searchParams).archived === "1";
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, canvasQuery(() => api, project, slug, archived));
  if (!error) {
    await Promise.all([prefetch(client, proposalsQuery(() => api, project, slug)), prefetch(client, baselinesQuery(() => api, project, slug))]);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CanvasPage project={project} slug={slug} archived={archived} initialError={error} />
    </HydrationBoundary>
  );
}
