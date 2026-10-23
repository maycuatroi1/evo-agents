"use client";

import { useQuery } from "@tanstack/react-query";
import { Puzzle } from "lucide-react";
import dynamic from "next/dynamic";
import type { Route } from "next";

import { browserApi } from "@/lib/api/browser";
import { menuPages, shownPage } from "@/lib/plugin-pages";
import { pluginPageHref, projectPluginsQuery } from "@/lib/plugin-queries";

import { isActive } from "./nav";

/**
 * The icon a page names, from a set of its own chunk (plugin-icons.tsx): a hub without plugins never loads it, and the
 * puzzle stands in while it loads.
 */
const PluginIcon = dynamic(() => import("./plugin-icons"), { ssr: false, loading: () => <Puzzle aria-hidden="true" /> });

/** A menu item a plugin's page adds: what the sidebar draws (app-sidebar.tsx), in its Apps fold. */
export type PluginNavLink = { key: string; testId: string; name: string; icon: React.ReactNode; href: Route; active: boolean };

/**
 * The items of the pages a project's enabled plugins add to its menu (web/DESIGN.md, Plugins), after the project's own
 * areas: each with the icon and the title its manifest gives, in the plugins' order. A plugin the project has not
 * enabled, or the hub turned off, adds nothing; nor does a page that asks for no menu item. A refusal (no grant) or a
 * failure leaves the menu without them.
 */
export function usePluginNavLinks(project: string | null, pathname: string): PluginNavLink[] {
  const { data } = useQuery({
    ...projectPluginsQuery(browserApi, project ?? ""),
    enabled: Boolean(project),
    retry: false,
    staleTime: 60_000,
  });
  if (!project) return [];
  return menuPages(data ?? []).map(({ plugin, page }) => {
    const href = pluginPageHref(project, plugin.name, page.name);
    return {
      key: `${plugin.name}/${page.name}`,
      testId: `nav-plugin-${plugin.name}-${page.name}`,
      name: page.title,
      icon: <PluginIcon name={page.icon} />,
      href,
      active: isActive(pathname, href, false),
    };
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
