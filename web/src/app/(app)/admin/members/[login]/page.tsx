import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import {
  adminUsersQuery,
  auditQuery,
  LOGIN_NAME,
  memberAuditParams,
  memberTokensParams,
  tokensQuery,
} from "@/components/admin/data";
import { AdminMember } from "@/components/admin/member-page";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { projectsQuery } from "@/lib/queries";

type Props = { params: Promise<{ login: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("admin.member");
  return { title: `${decodeURIComponent((await params).login)} | ${t("metaTitle")}` };
}

export default async function MemberPage({ params }: Props) {
  const login = decodeURIComponent((await params).login);
  if (!LOGIN_NAME.test(login)) {
    const t = await getTranslations("admin.member");
    return <NotFoundState title={t("notFoundTitle", { login })} description={t("notFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, adminUsersQuery(() => api)),
    prefetch(client, projectsQuery(() => api)),
    prefetch(client, tokensQuery(() => api, memberTokensParams(login))),
    prefetch(client, auditQuery(() => api, memberAuditParams(login))),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminMember login={login} initialError={error} />
    </HydrationBoundary>
  );
}
