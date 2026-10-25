import "server-only";

import { notFound } from "next/navigation";

import { PROJECT_NAME } from "@/lib/queries";

/** A task's ref as the hub takes it in a path: `T-42` (a prefix of the config, evo_agents.hub.server.tasks.PREFIX), or `42`. */
const REF = /^(?:[A-Z][A-Z0-9]{0,9}-)?\d{1,9}$/;

/** The project a route of the plugin tasks names, decoded; a 404 for a name the hub would refuse. */
export async function projectParam(params: Promise<{ project: string }>): Promise<string> {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  return project;
}

/** The project and the task a task's route names; a 404 for a ref the hub would refuse. */
export async function taskParams(params: Promise<{ project: string; ref: string }>): Promise<{ project: string; ref: string }> {
  const project = await projectParam(params);
  const ref = decodeURIComponent((await params).ref);
  if (!REF.test(ref)) notFound();
  return { project, ref };
}

/** One value of a search parameter, cut to `max` characters; null when it is not there. */
export function searchText(value: string | string[] | undefined, max = 100): string | null {
  const found = Array.isArray(value) ? value[0] : value;
  return found ? found.slice(0, max) : null;
}

/** The title the plugin's manifest gives page `page`, as the project's menu shows it. */
export function pageTitle(plugin: { pages: { name: string; title: string }[] } | null, page: string, fallback: string): string {
  return plugin?.pages.find((item) => item.name === page)?.title ?? fallback;
}
