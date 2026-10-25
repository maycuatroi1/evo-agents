"use client";

import { useQuery } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { pluginOn } from "@/lib/plugin-pages";
import { type PluginPage, type ProjectPlugin, projectPluginsQuery } from "@/lib/plugin-queries";

import { PluginFrame } from "./plugin-frame";

/** The slots a plugin's page may name instead of a page of its own (evo_agents.hub.server.plugin_pages). */
export type PageSlot = "decide";

/** Each enabled plugin's framed page whose slot is `slot`, in the order the hub lists the plugins and their pages. */
export function slotPages(plugins: readonly ProjectPlugin[], slot: PageSlot): { plugin: ProjectPlugin; page: PluginPage }[] {
  return plugins.filter(pluginOn).flatMap((plugin) =>
    plugin.pages.filter((page) => page.slot === slot && !page.native && page.url !== null).map((page) => ({ plugin, page })),
  );
}

/**
 * The panels of slot `slot` on a page of the hub (web/DESIGN.md, Plugins): each enabled plugin's page of that slot in
 * the plugin's frame, which takes no room until the page reports a height above 0. Nothing at all in a project whose
 * plugins have no such page. They are siblings in the caller's layout, so a panel that shows nothing adds no gap.
 */
export function SlotPanels({ project, slot }: { project: string; slot: PageSlot }) {
  const { data } = useQuery(projectPluginsQuery(browserApi, project));
  if (!data) return null;
  return (
    <>
      {slotPages(data, slot).map(({ plugin, page }) => (
        <PluginFrame key={`${plugin.name}/${page.name}`} project={project} plugin={plugin} page={page} panel />
      ))}
    </>
  );
}
