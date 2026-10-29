import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { pluginPageHref } from "@/lib/plugin-queries";

import { PAGE, PLUGIN } from "./api";

/** Where the pages of the core plugin canvas are, under its page in the project's menu. */
export function canvasesHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, PAGE);
}

/** A canvas's grid; with archived claims shown when `archived`. */
export function canvasHref(project: string, slug: string, archived = false): Route {
  return `${canvasesHref(project)}/${encodeURIComponent(slug)}${archived ? "?archived=1" : ""}` as Route;
}

/** A claim's page, by its code (`CL-3`). */
export function claimHref(project: string, slug: string, code: string): Route {
  return `${canvasHref(project, slug)}/claims/${encodeURIComponent(code)}` as Route;
}

/** A baseline's review, the page the notice canvas_baseline links (evo_agents.hub.server.canvas.notices.link). */
export function baselineHref(project: string, slug: string, number: number): Route {
  return `${canvasHref(project, slug)}/baselines/${number}` as Route;
}

/** The plugin's page in the project (its config: the weekly baseline's day, time and zone). */
export function settingsHref(project: string): Route {
  return `/p/${encodeURIComponent(project)}/plugins/${PLUGIN}` as Route;
}

/** An agent run's page, the run a proposal came from. */
export function runHref(project: string, id: number): Route {
  return projectHref(project, `runs/${id}`);
}
