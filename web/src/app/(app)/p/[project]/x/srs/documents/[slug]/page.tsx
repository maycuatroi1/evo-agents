import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { srsDocumentQuery } from "@/components/srs/api";
import { DocumentPage } from "@/components/srs/document-page";
import { shownNumber, versionParam } from "@/components/srs/model";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../route-params";

type Props = {
  params: Promise<{ project: string; slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("document", await documentParams(params)) };
}

function text(value: string | string[] | undefined): string | null {
  const found = Array.isArray(value) ? value[0] : value;
  return found ? found.slice(0, 100) : null;
}

/**
 * A document's contents at the version asked (`v`) or the approved one, the item `s` open, its editor with `edit`. The
 * latest version comes first, since it says which version is approved; the version shown follows when it differs.
 */
export default async function SrsDocumentRoute({ params, searchParams }: Props) {
  const { project, slug } = await documentParams(params);
  const query = await searchParams;
  const asked = { version: versionParam(query.v), item: text(query.s), edit: text(query.edit) === "1" };
  const api = await serverApi();
  const client = getQueryClient();
  const latest = srsDocumentQuery(() => api, project, slug, null);
  const error = await prefetch(client, latest);
  const held = client.getQueryData(latest.queryKey);
  const shown = held ? shownNumber(held, asked.version) : null;
  if (held && shown !== null && shown !== held.latest_version) await prefetch(client, srsDocumentQuery(() => api, project, slug, shown));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <DocumentPage project={project} slug={slug} asked={asked} initialError={error} />
    </HydrationBoundary>
  );
}
