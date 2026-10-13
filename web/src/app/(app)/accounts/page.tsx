import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AccountsPage } from "@/components/accounts/accounts-page";
import { accountListQuery } from "@/components/accounts/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("accounts");
  return { title: t("title") };
}

/** The visitor's own accounts with their figures and their rotation policy by project, a hub admin's too; never a value. */
export default async function AccountsRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, accountListQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AccountsPage initialError={error} />
    </HydrationBoundary>
  );
}
