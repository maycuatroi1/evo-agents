import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ProjectsHome } from "@/components/pages/projects-home";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { projectsQuery } from "@/lib/queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("home");
  return { title: t("title") };
}

export default async function HomePage() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, projectsQuery(() => api)); // shared with the layout's fetch
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProjectsHome initialError={error} />
    </HydrationBoundary>
  );
}
