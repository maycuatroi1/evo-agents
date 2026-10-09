import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MonitorPage } from "@/components/monitor/monitor-page";
import { MAX_TILES, parseTiles, RUNS_PARAM } from "@/components/monitor/model";
import { runQuery } from "@/components/runs/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { overviewQuery } from "@/lib/queries";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("monitor");
  return { title: t("title") };
}

/**
 * The Monitor: the runs in flight of every project of the member's grants (GET /v1/me/overview) and the runs the URL
 * names (`?runs=project:id,...`), read on the server so the list and the tiles' heads render complete; each tile then
 * follows its run's trace in the browser. A run the member cannot read is not shown.
 */
export default async function MonitorRoute({ searchParams }: Props) {
  const param = (await searchParams)[RUNS_PARAM];
  const tiles = (parseTiles(typeof param === "string" ? param : null) ?? []).slice(0, MAX_TILES);
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, overviewQuery(() => api)),
    ...tiles.map((tile) => prefetch(client, runQuery(() => api, tile.project, tile.id))),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MonitorPage initialError={error} />
    </HydrationBoundary>
  );
}
