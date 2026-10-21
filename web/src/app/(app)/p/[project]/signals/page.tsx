import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { queryOf, seriesFilter } from "@/components/signals/model";
import { SignalsPage } from "@/components/signals/signals-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";
import { signalKeysQuery, signalsQuery } from "@/lib/signal-queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("signals.page");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/**
 * A project's signals, for any member with a grant on it: the keys of its registry, and the series of the key the
 * address names (`?key=&tenant=&days=`, the link of the value check's evidence), both read on the server.
 */
export default async function ProjectSignalsRoute({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const query = queryOf(await searchParams);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, signalKeysQuery(() => api, project));
  const seriesError = !error && query.key ? await prefetch(client, signalsQuery(() => api, project, seriesFilter(query))) : null;
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SignalsPage project={project} query={query} initialError={error} seriesError={seriesError} />
    </HydrationBoundary>
  );
}
