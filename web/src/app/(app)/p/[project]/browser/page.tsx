import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { identitiesQuery } from "@/components/browser/queries";
import { IdentitiesPage } from "@/components/browser/identities-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }>; searchParams: Promise<{ login?: string | string[] }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("browser");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/**
 * A project's browser identities the visitor may use, read on the server so the page renders complete. `?login=ID`,
 * the link of a needs-login notice, marks the identity to log in.
 */
export default async function ProjectBrowserPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const raw = (await searchParams).login;
  const asked = Number(Array.isArray(raw) ? raw[0] : raw);
  const login = Number.isInteger(asked) && asked > 0 ? asked : null;
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, identitiesQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <IdentitiesPage project={project} login={login} initialError={error} />
    </HydrationBoundary>
  );
}
