import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";

import { ProjectOverview } from "@/components/pages/project-overview";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: decodeURIComponent((await params).project) };
}

export default async function ProjectPage({ params }: Props) {
  const name = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(name)) return <ProjectOverview name={name} initialError={null} invalid />;
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, projectQuery(() => api, name));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProjectOverview name={name} initialError={error} />
    </HydrationBoundary>
  );
}
