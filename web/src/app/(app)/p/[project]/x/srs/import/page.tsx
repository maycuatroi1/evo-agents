import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { srsDocumentsQuery } from "@/components/srs/api";
import { ImportPage } from "@/components/srs/import-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

import { pageTitle } from "../route-params";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("import", { project: decodeURIComponent((await params).project) }) };
}

/** Markdown into the project's BA documents: a new document, or the next version of `doc`. */
export default async function SrsImportRoute({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const plugin = await requirePlugin("srs", project, "import");
  const doc = (await searchParams).doc;
  const target = typeof doc === "string" && /^[a-z0-9][a-z0-9-]{0,99}$/.test(doc) ? doc : null;
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, srsDocumentsQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ImportPage project={project} title={pageTitle(plugin, "import", "Import SRS")} target={target} initialError={error} />
    </HydrationBoundary>
  );
}
