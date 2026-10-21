import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { newTaskHref, taskHref, TASKS_BOARD } from "@/lib/core-plugin-links";
import { pluginPageHref, projectPluginHref } from "@/lib/plugin-queries";

import { PLUGIN } from "./api";
import type { BoardAsk } from "./model";

/**
 * Where the Tasks pages are, under the plugin's native pages (web/DESIGN.md, Plugins): /p/{project}/x/tasks/board is
 * the board of a sprint (`?sprint=` names it, `?backlog=1` asks for the tasks of no sprint) and holds each task's page
 * (`/board/T-42`) and the form of a new one (`/board/new`); /backlog, /mine and /import are the other tabs.
 */
export { newTaskHref, taskHref };

export function boardHref(project: string, of: BoardAsk = null): Route {
  const base = pluginPageHref(project, PLUGIN, TASKS_BOARD);
  if (of && "sprint" in of) return `${base}?${new URLSearchParams({ sprint: of.sprint }).toString()}` as Route;
  if (of) return `${base}?backlog=1` as Route;
  return base;
}

export function backlogHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, "backlog");
}

export function mineHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, "mine");
}

export function importHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, "import");
}

/** The plugin's page in the project, where its admins set the prefix, statuses, types and on_steps_done. */
export function settingsHref(project: string): Route {
  return projectPluginHref(project, PLUGIN);
}

export function planStepHref(project: string, plan: string, step: string): Route {
  return projectHref(project, `plans/${encodeURIComponent(plan)}/steps/${encodeURIComponent(step)}`);
}

export function runHref(project: string, id: number): Route {
  return projectHref(project, `runs/${id}`);
}
