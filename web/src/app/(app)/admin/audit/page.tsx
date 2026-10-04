import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTimeZone, getTranslations } from "next-intl/server";

import { AdminAudit } from "@/components/admin/audit-page";
import {
  adminUsersQuery,
  auditActionsQuery,
  auditParams,
  auditQuery,
  fromRecord,
  parseAuditFilters,
} from "@/components/admin/data";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { projectsQuery } from "@/lib/queries";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("admin.audit");
  return { title: t("title") };
}

/** Days in the filters are whole days in the hub's display time zone, on the server as in the browser. */
export default async function AuditPage({ searchParams }: Props) {
  const filters = parseAuditFilters(fromRecord(await searchParams));
  const timeZone = await getTimeZone();
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, auditQuery(() => api, auditParams(filters, timeZone))),
    prefetch(client, auditActionsQuery(() => api)),
    prefetch(client, adminUsersQuery(() => api)),
    prefetch(client, projectsQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminAudit initialError={error} />
    </HydrationBoundary>
  );
}
