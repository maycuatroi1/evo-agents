import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { canvasesQuery, PAGE, PLUGIN } from "@/components/canvas/api";
import { CanvasesPage } from "@/components/canvas/canvases-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

import { pageTitle } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("canvas.meta");
  return { title: t("canvases", { project: decodeURIComponent((await params).project) }) };
}

/** The Lean Canvases of a project that enabled the core plugin canvas; the layout has checked it did. */
export default async function CanvasesRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const plugin = await requirePlugin(PLUGIN, project, PAGE);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, canvasesQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CanvasesPage project={project} title={pageTitle(plugin, PAGE, "Lean Canvas")} initialError={error} />
    </HydrationBoundary>
  );
}
