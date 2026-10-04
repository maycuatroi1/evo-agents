import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { blobStoreQuery, SKILL_NAME, skillQuery } from "@/components/skills/queries";
import { SkillDetail } from "@/components/skills/skill-detail";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { params: Promise<{ name: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: decodeURIComponent((await params).name) };
}

/** A global skill: every version, its source, and its bundle to download. */
export default async function GlobalSkillPage({ params }: Props) {
  const name = decodeURIComponent((await params).name);
  if (!SKILL_NAME.test(name)) {
    const t = await getTranslations("skills.detail");
    return <NotFoundState title={t("notFoundTitle", { name })} description={t("notFoundDescription")} />;
  }
  const place = { kind: "global" } as const;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, skillQuery(() => api, place, name)),
    prefetch(client, blobStoreQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <SkillDetail place={place} name={name} initialError={error} />
    </HydrationBoundary>
  );
}
