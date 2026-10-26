import type { Route } from "next";

import { pluginPageHref, projectPluginHref } from "@/lib/plugin-queries";

import { PLUGIN } from "./api";

/**
 * Where the Docs pages are, under the feature's native pages (web/DESIGN.md, Plugins): /p/{project}/x/docs/documents
 * lists the documents and holds each document's pages (read, history, edit); /p/{project}/x/docs/new makes one.
 */
export function documentsHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, "documents");
}

export function newDocumentHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, "new");
}

/** The feature's page in the project, where its admins turn it off. */
export function settingsHref(project: string): Route {
  return projectPluginHref(project, PLUGIN);
}

function documentBase(project: string, slug: string): string {
  return `${documentsHref(project)}/${encodeURIComponent(slug)}`;
}

/** A document read at version `v`, the latest when left out. */
export function documentHref(project: string, slug: string, v?: number | null): Route {
  return `${documentBase(project, slug)}${v ? `?v=${v}` : ""}` as Route;
}

export function historyHref(project: string, slug: string): Route {
  return `${documentBase(project, slug)}/history` as Route;
}

export function editHref(project: string, slug: string): Route {
  return `${documentBase(project, slug)}/edit` as Route;
}
