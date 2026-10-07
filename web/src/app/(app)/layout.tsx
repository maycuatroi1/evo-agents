import { dehydrate } from "@tanstack/react-query";
import { cookies } from "next/headers";
import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { ShellError } from "@/components/shell/shell-error";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

/**
 * The signed-in shell: who the visitor is and which projects the API returns for them, fetched on the server with
 * the visitor's cookie so the sidebar renders complete. A 401 from either call goes to the sign-in page.
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const [api, cookieStore] = await Promise.all([serverApi(), cookies()]);
  const client = getQueryClient();
  // A failed project list is not fatal: the picker and the pages that need it ask again from the browser.
  const [meError] = await Promise.all([
    prefetch(client, whoamiQuery(() => api)),
    prefetch(client, projectsQuery(() => api)),
  ]);
  if (meError) return <ShellError error={meError} />;
  const sidebarOpen = cookieStore.get("sidebar_state")?.value !== "false";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AppShell sidebarOpen={sidebarOpen}>{children}</AppShell>
    </HydrationBoundary>
  );
}
