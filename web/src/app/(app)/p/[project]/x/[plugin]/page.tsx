import { notFound, redirect } from "next/navigation";

import { isApiError } from "@/lib/api/errors";
import { getQueryClient } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { menuPages, shownPage } from "@/lib/plugin-pages";
import { PLUGIN_NAME, type ProjectPlugin, pluginPageHref, projectPluginQuery } from "@/lib/plugin-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; plugin: string }> };

/** The page /p/{project}/x/{plugin} leads to: the plugin's first page in the menu, else its first page that shows. */
function firstPage(plugin: ProjectPlugin): string | null {
  const listed = menuPages([plugin])[0];
  if (listed) return listed.page.name;
  return plugin.pages.find((page) => shownPage(plugin, page.name) !== null)?.name ?? null;
}

/**
 * /p/{project}/x/{plugin}: the plugin's first page (web/DESIGN.md, Plugins). A 404 where the project has not enabled
 * the plugin, the hub turned it off, or it has no page that shows; a core plugin's native pages have a route of their
 * own under x/<plugin>, which wins over this one. Any other failure (no grant, the API out of reach) is the route's
 * error, as no page can be chosen without the plugin.
 */
export default async function PluginIndexRoute({ params }: Props) {
  const { project: rawProject, plugin: rawPlugin } = await params;
  const project = decodeURIComponent(rawProject);
  const name = decodeURIComponent(rawPlugin);
  if (!PROJECT_NAME.test(project) || !PLUGIN_NAME.test(name)) notFound();
  let found: ProjectPlugin;
  try {
    const api = await serverApi();
    found = await getQueryClient().fetchQuery(projectPluginQuery(() => api, project, name));
  } catch (error) {
    if (isApiError(error) && error.kind === "not_found") notFound();
    throw error;
  }
  const page = firstPage(found);
  if (page === null) notFound();
  redirect(pluginPageHref(project, name, page));
}
