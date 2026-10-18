import "server-only";

import { notFound } from "next/navigation";

import { isApiError } from "@/lib/api/errors";
import { getQueryClient } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { shownPage } from "@/lib/plugin-pages";
import { type ProjectPlugin, projectPluginQuery } from "@/lib/plugin-queries";

/**
 * The guard of a plugin's pages (web/DESIGN.md, Plugins): a core plugin's native pages call it from the layout of
 * src/app/(app)/p/[project]/x/<plugin>, and the framed pages from their route. A page is a 404 where the project has
 * not enabled the plugin, the hub turned it off, or the plugin has no such page, as the plugin's API routes answer 404
 * there. The plugin is read through the request's QueryClient, which the page hands to the browser. Any other failure
 * (no grant, the API out of reach) lets the page render and show it through its own query; null then.
 */
export async function requirePlugin(plugin: string, project: string, page: string): Promise<ProjectPlugin | null> {
  let found: ProjectPlugin;
  try {
    const api = await serverApi();
    found = await getQueryClient().fetchQuery(projectPluginQuery(() => api, project, plugin));
  } catch (error) {
    if (isApiError(error) && error.kind === "not_found") notFound();
    return null;
  }
  if (shownPage(found, page) === null) notFound();
  return found;
}
