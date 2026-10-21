import type { PluginPage, ProjectPlugin } from "@/lib/plugin-queries";

/**
 * The pages plugins add to a project's part of the web (docs/plugins.md, Pages). A page shows while its plugin is
 * enabled in the project and the hub has not turned it off; an isolated plugin's page is framed from the URL the hub
 * signed for it (`url`), a core plugin's is native, a page of this web under src/app/(app)/p/[project]/x/<plugin>.
 */
export type ShownPage = { plugin: ProjectPlugin; page: PluginPage };

/** Whether `plugin` works in the project now: enabled there and not turned off on the hub. */
export function pluginOn(plugin: ProjectPlugin): boolean {
  return plugin.enabled && plugin.state === "installed";
}

/** Whether a page can show: a native page always can, a framed one once the hub signed its URL. */
function showable(page: PluginPage): boolean {
  return page.native || page.url !== null;
}

/** The pages the project's menu lists, plugin by plugin in the hub's order, each in its manifest's order. */
export function menuPages(plugins: readonly ProjectPlugin[]): ShownPage[] {
  return plugins
    .filter(pluginOn)
    .flatMap((plugin) => plugin.pages.filter((page) => page.menu && showable(page)).map((page) => ({ plugin, page })));
}

/** The page `name` of `plugin` when the project shows it; null when it does not exist or is not on there. */
export function shownPage(plugin: ProjectPlugin | undefined, name: string): PluginPage | null {
  if (!plugin || !pluginOn(plugin)) return null;
  const page = plugin.pages.find((item) => item.name === name);
  return page && showable(page) ? page : null;
}

/** The permissions a plugin asks for that the project has not granted it yet, after an upgrade asked for more. */
export function pendingPermissions(plugin: ProjectPlugin): string[] {
  return plugin.enabled ? plugin.pending : [];
}
