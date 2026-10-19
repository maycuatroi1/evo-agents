"use client";

import { useQuery } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { shownPage } from "@/lib/plugin-pages";
import { projectPluginQuery } from "@/lib/plugin-queries";

/**
 * Whether the project shows page `page` of plugin `plugin` now: the plugin is enabled there, the hub has not turned it
 * off, and the page exists (lib/plugin-pages.ts). False while the answer is on its way and when the hub refuses it,
 * so a link to the page shows only once it would open.
 */
export function usePluginPageOn(project: string, plugin: string, page: string): boolean {
  const { data } = useQuery({ ...projectPluginQuery(browserApi, project, plugin), retry: false });
  return shownPage(data, page) !== null;
}
