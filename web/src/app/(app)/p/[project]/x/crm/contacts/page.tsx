import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { CRM_CONTACTS, CRM_PLUGIN, crmContactsQuery } from "@/components/crm/api";
import { ContactsPage } from "@/components/crm/contacts-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

async function projectOf(params: Props["params"]): Promise<string> {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  return project;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("crm.meta");
  return { title: t("contacts", { project: await projectOf(params) }) };
}

/** The contacts of the project and its pipeline by stage, read on the server so the page renders complete. */
export default async function CrmContactsRoute({ params }: Props) {
  const project = await projectOf(params);
  const plugin = await requirePlugin(CRM_PLUGIN, project, CRM_CONTACTS);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, crmContactsQuery(() => api, project));
  const title = plugin?.pages.find((item) => item.name === CRM_CONTACTS)?.title ?? "Contacts";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ContactsPage project={project} title={title} initialError={error} />
    </HydrationBoundary>
  );
}
