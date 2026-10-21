"use client";

import { useQuery } from "@tanstack/react-query";
import { Puzzle } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";

import { SidebarMenuButton, SidebarMenuItem } from "@/components/ui/sidebar";
import { browserApi } from "@/lib/api/browser";
import { menuPages, shownPage } from "@/lib/plugin-pages";
import { pluginPageHref, projectPluginsQuery } from "@/lib/plugin-queries";

import { isActive } from "./nav";

/**
 * The icon a page names, from a set of its own chunk (plugin-icons.tsx): a hub without plugins never loads it, and the
 * puzzle stands in while it loads.
 */
const PluginIcon = dynamic(() => import("./plugin-icons"), { ssr: false, loading: () => <Puzzle aria-hidden="true" /> });

/** The project's plugins as the menu reads them: a refusal (no grant) or a failure leaves the menu without them. */
function useProjectPlugins(project: string) {
  return useQuery({ ...projectPluginsQuery(browserApi, project), retry: false, staleTime: 60_000 });
}

/**
 * The items of the pages a project's enabled plugins add to its menu (web/DESIGN.md, Plugins), after the project's own
 * areas: each with the icon and the title its manifest gives, in the plugins' order. A plugin the project has not
 * enabled, or the hub turned off, adds nothing; nor does a page that asks for no menu item.
 */
export function PluginNavItems({ project, pathname, onNavigate }: { project: string; pathname: string; onNavigate: () => void }) {
  const { data } = useProjectPlugins(project);
  return menuPages(data ?? []).map(({ plugin, page }) => {
    const href = pluginPageHref(project, plugin.name, page.name);
    const active = isActive(pathname, href, false);
    return (
      <SidebarMenuItem key={`${plugin.name}/${page.name}`}>
        <SidebarMenuButton asChild isActive={active} tooltip={page.title}>
          <Link
            href={href}
            aria-current={active ? "page" : undefined}
            onClick={onNavigate}
            data-testid={`nav-plugin-${plugin.name}-${page.name}`}
          >
            <PluginIcon name={page.icon} />
            <span className="min-w-0 flex-1 truncate">{page.title}</span>
          </Link>
        </SidebarMenuButton>
      </SidebarMenuItem>
    );
  });
}

/** The title of a plugin's page in the project, for the breadcrumb; null until the plugins are read. */
export function usePluginPageTitle(project: string | null, plugin: string | undefined, page: string | undefined): string | null {
  const { data } = useQuery({
    ...projectPluginsQuery(browserApi, project ?? ""),
    enabled: Boolean(project && plugin && page),
    retry: false,
    staleTime: 60_000,
  });
  if (!plugin || !page) return null;
  return shownPage(data?.find((item) => item.name === plugin), page)?.title ?? null;
}
