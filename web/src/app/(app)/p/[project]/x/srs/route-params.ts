import "server-only";

import { notFound } from "next/navigation";

import { PROJECT_NAME } from "@/lib/queries";

/** A document's slug, as the hub takes it (evo_agents.hub.tables_srs.SLUG). */
const SLUG = /^[a-z0-9][a-z0-9-]{0,99}$/;

/** The project and document a route of the plugin srs names, decoded; a 404 for a name the hub would refuse. */
export async function documentParams(params: Promise<{ project: string; slug: string }>) {
  const { project, slug } = await params;
  const names = { project: decodeURIComponent(project), slug: decodeURIComponent(slug) };
  if (!PROJECT_NAME.test(names.project) || !SLUG.test(names.slug)) notFound();
  return names;
}

/** The title the plugin's manifest gives page `page`, as the project's menu shows it. */
export function pageTitle(plugin: { pages: { name: string; title: string }[] } | null, page: string, fallback: string): string {
  return plugin?.pages.find((item) => item.name === page)?.title ?? fallback;
}
