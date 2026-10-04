import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { adminUsersQuery } from "@/components/admin/data";
import { AdminMembers } from "@/components/admin/members-page";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { projectsQuery } from "@/lib/queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin.members");
  return { title: t("title") };
}

/** Hub admins only: the API answers 403 to anyone else, and the page shows that state. */
export default async function MembersPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, adminUsersQuery(() => api)),
    prefetch(client, projectsQuery(() => api)), // shared with the layout's fetch
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminMembers initialError={error} />
    </HydrationBoundary>
  );
}
