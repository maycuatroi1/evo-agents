import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { skillsQuery } from "@/components/skills/queries";
import { SkillsBrowser } from "@/components/skills/skills-browser";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("skills");
  return { title: t("globalTitle") };
}

/** The hub's global skills, which every signed-in member reads. */
export default async function GlobalSkillsPage() {
  const place = { kind: "global" } as const;
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, skillsQuery(() => api, place));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SkillsBrowser place={place} initialError={error} />
    </HydrationBoundary>
  );
}
