import type { Route } from "next";

import { pluginPageHref } from "@/lib/plugin-queries";

/**
 * Where the native pages of the core plugins srs and tasks link to each other (plan srs-tasks-plugins): an SRS item's
 * page opens the form of a new task linked to it and its trace opens a task's page; a task's page opens the SRS items
 * it links to. Each feature folder keeps its own pages' links (components/srs/links.ts, components/tasks/links.ts);
 * these are the ones the other folder needs, which neither may import from the other.
 */
export const SRS_PLUGIN = "srs";
export const TASKS_PLUGIN = "tasks";
/** The tasks plugin's page in the project's menu: the board, with a task's page and a new task under it. */
export const TASKS_BOARD = "board";

/** A task's page, by its ref as shown (`T-42`). */
export function taskHref(project: string, ref: string): Route {
  return `${pluginPageHref(project, TASKS_PLUGIN, TASKS_BOARD)}/${encodeURIComponent(ref)}` as Route;
}

/** The form of a new task, with `link` (an SRS code, a plan step) and `sprint` filled in when given. */
export function newTaskHref(project: string, given: { link?: string | null; sprint?: string | null } = {}): Route {
  const search = new URLSearchParams();
  if (given.link) search.set("link", given.link);
  if (given.sprint) search.set("sprint", given.sprint);
  const query = search.toString();
  return `${pluginPageHref(project, TASKS_PLUGIN, TASKS_BOARD)}/new${query ? `?${query}` : ""}` as Route;
}

/** An SRS item's page in its document, by code. */
export function srsItemHref(project: string, slug: string, code: string): Route {
  return `${pluginPageHref(project, SRS_PLUGIN, "documents")}/${encodeURIComponent(slug)}/items/${encodeURIComponent(code)}` as Route;
}

/** The ref of the task a Ticket node of the knowledge graph is (`plugin.tasks:ticket:T-42`), or null for another id. */
export function ticketRef(nodeId: string): string | null {
  const found = /^plugin\.tasks:ticket:([A-Z][A-Z0-9]{0,9}-\d+)$/.exec(nodeId);
  return found ? found[1] : null;
}
