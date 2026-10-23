import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { MARKETING_POSTS, MARKETING_PLUGIN, marketingPostsQuery } from "@/components/marketing/api";
import { PostsPage } from "@/components/marketing/posts-page";
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
  const t = await getTranslations("marketing.meta");
  return { title: t("posts", { project: await projectOf(params) }) };
}

/** The posts of the project by what the gate did with them; a reader's read answers 403 and the page shows a note instead. */
export default async function MarketingPostsRoute({ params }: Props) {
  const project = await projectOf(params);
  const plugin = await requirePlugin(MARKETING_PLUGIN, project, MARKETING_POSTS);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, marketingPostsQuery(() => api, project));
  const title = plugin?.pages.find((item) => item.name === MARKETING_POSTS)?.title ?? "Posts";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PostsPage project={project} title={title} initialError={error} />
    </HydrationBoundary>
  );
}
