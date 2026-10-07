import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { adminStatsQuery } from "@/components/admin/data";
import { AdminDiagnosticsPage } from "@/components/admin/diagnostics-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin.diagnostics");
  return { title: t("title") };
}

/** The rows of every hub table, for hub admins; anyone else gets the API's 403 as the no-access state. */
export default async function DiagnosticsPage() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, adminStatsQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminDiagnosticsPage initialError={error} />
    </HydrationBoundary>
  );
}
