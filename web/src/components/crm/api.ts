import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { pluginPageHref } from "@/lib/plugin-queries";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of the core plugin crm (evo_agents.core_plugins.crm.server, /v1/x/crm/{project}), as TanStack
 * Query options the server (prefetch) and the browser share. A reader reads the contacts; a writer adds a note; only an
 * admin deletes a contact, with its touchpoints and stage history, on that person's request.
 */
type Schemas = components["schemas"];
export type CrmContacts = Schemas["CrmContacts"];
export type CrmContact = Schemas["CrmContact"];
export type CrmContactDetail = Schemas["CrmContactDetail"];
export type CrmTouchpoint = Schemas["CrmTouchpoint"];

export const CRM_PLUGIN = "crm";
/** The plugin's page in the project's menu. */
export const CRM_CONTACTS = "contacts";
/** The stages in the order a contact moves through them (evo_agents.hub.tables_crm.STAGES). */
export const CRM_STAGES = ["lead", "invited", "activated", "retained"] as const;
export type CrmStage = (typeof CRM_STAGES)[number];

export function isStage(value: string | null | undefined): value is CrmStage {
  return CRM_STAGES.includes(value as CrmStage);
}

export const crmKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", CRM_PLUGIN] as const,
  contacts: (project: string) => [...crmKeys.all(project), "contacts"] as const,
  contact: (project: string, login: string) => [...crmKeys.contacts(project), "one", login] as const,
};

export const crmContactsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: crmKeys.contacts(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/crm/{project}/contacts", { params: { path: { project } }, signal })),
  });

export const crmContactQuery = (api: ApiSource, project: string, login: string) =>
  queryOptions({
    queryKey: crmKeys.contact(project, login),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/crm/{project}/contacts/{login}", { params: { path: { project, login } }, signal })),
  });

export async function addNote(api: ApiClient, project: string, login: string, note: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/crm/{project}/contacts/{login}/notes", { params: { path: { project, login } }, body: { note }, headers }));
}

export async function deleteContact(api: ApiClient, project: string, login: string) {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/x/crm/{project}/contacts/{login}", { params: { path: { project, login } }, headers }));
}

export function contactsHref(project: string): Route {
  return pluginPageHref(project, CRM_PLUGIN, CRM_CONTACTS);
}

export function contactHref(project: string, login: string): Route {
  return `${contactsHref(project)}/${encodeURIComponent(login)}` as Route;
}
