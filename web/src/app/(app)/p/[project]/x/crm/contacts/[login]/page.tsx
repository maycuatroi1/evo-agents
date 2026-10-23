import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { crmContactQuery } from "@/components/crm/api";
import { ContactPage } from "@/components/crm/contact-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; login: string }> };

async function partsOf(params: Props["params"]): Promise<{ project: string; login: string }> {
  const raw = await params;
  const project = decodeURIComponent(raw.project);
  const login = decodeURIComponent(raw.login).toLowerCase();
  if (!PROJECT_NAME.test(project) || !/^[a-z0-9-]{1,39}$/.test(login)) notFound();
  return { project, login };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("crm.meta");
  return { title: t("contact", await partsOf(params)) };
}

/** One contact, its touchpoints and the stages it reached; the layout already checked the plugin is on. */
export default async function CrmContactRoute({ params }: Props) {
  const { project, login } = await partsOf(params);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, crmContactQuery(() => api, project, login));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ContactPage project={project} login={login} initialError={error} />
    </HydrationBoundary>
  );
}
