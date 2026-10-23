import { dehydrate } from "@tanstack/react-query";
import { cookies } from "next/headers";
import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { ShellError } from "@/components/shell/shell-error";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { brandingQuery, featuresQuery } from "@/lib/hub-settings";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

/**
 * The signed-in shell: who the visitor is, which projects the API returns for them, the hub's branding and which
 * features are on, fetched on the server with the visitor's cookie so the sidebar renders complete. A 401 from any
 * call goes to the sign-in page.
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const [api, cookieStore] = await Promise.all([serverApi(), cookies()]);
  const client = getQueryClient();
  // A failed project list, branding or features is not fatal: the parts that need them ask again from the browser,
  // and until they answer the sidebar shows the web's own name and mark and every entry.
  const [meError] = await Promise.all([
    prefetch(client, whoamiQuery(() => api)),
    prefetch(client, projectsQuery(() => api)),
    prefetch(client, brandingQuery(() => api)),
    prefetch(client, featuresQuery(() => api)),
  ]);
  if (meError) return <ShellError error={meError} />;
  const sidebarOpen = cookieStore.get("sidebar_state")?.value !== "false";
  // The sidebar's open folds (shell/nav.ts, NAV_GROUPS_COOKIE), so it renders as the visitor left it.
  const navGroups = cookieStore.get("hub_nav_groups")?.value;
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AppShell sidebarOpen={sidebarOpen} navGroups={navGroups}>
        {children}
      </AppShell>
    </HydrationBoundary>
  );
}
