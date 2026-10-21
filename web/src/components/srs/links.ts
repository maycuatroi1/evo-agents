import type { Route } from "next";

import { pluginPageHref, projectPluginHref } from "@/lib/plugin-queries";

import { PLUGIN } from "./api";

/**
 * Where the SRS pages are, under the plugin's native pages (web/DESIGN.md, Plugins): /p/{project}/x/srs/documents
 * lists the documents and holds each document's pages; /p/{project}/x/srs/import imports Markdown. A code or a key
 * travels in the path, encoded, since every code the templates allow is a path segment once encoded.
 */
function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== false) search.set(name, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function documentsHref(project: string): Route {
  return pluginPageHref(project, PLUGIN, "documents");
}

export function importHref(project: string, document?: string | null): Route {
  return `${pluginPageHref(project, PLUGIN, "import")}${query({ doc: document })}` as Route;
}

/** The plugin's page in the project, where its admins set the template, the roles and the approval policy. */
export function settingsHref(project: string): Route {
  return projectPluginHref(project, PLUGIN);
}

function documentBase(project: string, slug: string): string {
  return `${documentsHref(project)}/${encodeURIComponent(slug)}`;
}

/** A document's contents: version `v` (the approved one when left out), item `s` open, its editor with `edit`. */
export function documentHref(
  project: string,
  slug: string,
  at: { v?: number | null; s?: string | null; edit?: boolean } = {},
): Route {
  return `${documentBase(project, slug)}${query({ v: at.v, s: at.s, edit: at.edit ? 1 : null })}` as Route;
}

export function itemHref(project: string, slug: string, ref: string, version?: number | null): Route {
  return `${documentBase(project, slug)}/items/${encodeURIComponent(ref)}${query({ v: version })}` as Route;
}

export function versionsHref(project: string, slug: string): Route {
  return `${documentBase(project, slug)}/versions` as Route;
}

/** A version's review: its approvals, what it changes and the reviewer's form. */
export function reviewHref(project: string, slug: string, number: number): Route {
  return `${documentBase(project, slug)}/versions/${number}` as Route;
}

export function compareHref(project: string, slug: string, from: number, to: number): Route {
  return `${documentBase(project, slug)}/compare${query({ from, to })}` as Route;
}

export function commentsHref(project: string, slug: string): Route {
  return `${documentBase(project, slug)}/comments` as Route;
}
