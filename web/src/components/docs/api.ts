import { queryOptions } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { fromResponse, networkError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of the hub's feature docs (evo_agents.hub.server.docs.server, under /v1/x/docs/{project}/), as
 * TanStack Query options the server (prefetch) and the browser share, and the writes the browser makes with the
 * session's X-Evo-CSRF header. Every key sits under the project's plugin keys, so one invalidation of a document
 * reloads every version of it.
 */
type Schemas = components["schemas"];
export type DocsDocumentSummary = Schemas["DocsDocumentSummary"];
export type DocsDocument = Schemas["DocsDocument"];
export type DocsVersion = Schemas["DocsVersion"];
export type DocsVersionDetail = Schemas["DocsVersionDetail"];
export type DocsImage = Schemas["DocsImage"];
export type DocsSave = Schemas["DocsSave"];
export type DocsSaved = Schemas["DocsSaved"];
export type DocsUploadedImage = Schemas["DocsUploadedImage"];

export const PLUGIN = "docs";

export const docsKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", PLUGIN] as const,
  documents: (project: string) => [...docsKeys.all(project), "documents"] as const,
  document: (project: string, slug: string) => [...docsKeys.documents(project), slug] as const,
  version: (project: string, slug: string, version: number | null) =>
    [...docsKeys.document(project, slug), "version", version ?? "latest"] as const,
};

export const docsDocumentsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: docsKeys.documents(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/docs/{project}/documents", { params: { path: { project } }, signal })),
  });

/** A document with its versions and one of them whole: `version`, or the latest when null. */
export const docsDocumentQuery = (api: ApiSource, project: string, slug: string, version: number | null) =>
  queryOptions({
    queryKey: docsKeys.version(project, slug, version),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/docs/{project}/documents/{slug}", {
          params: { path: { project, slug }, query: version === null ? {} : { version } },
          signal,
        }),
      ),
  });

export async function saveDocument(api: ApiClient, project: string, slug: string, body: DocsSave): Promise<DocsSaved> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/x/docs/{project}/documents/{slug}", { params: { path: { project, slug } }, body, headers }));
}

/**
 * The upload of one image to the project's documents: `POST /v1/x/docs/{project}/images` with the file's bytes as the
 * body, on the hub's own origin (/v1, so the CSP's connect-src 'self' holds). The hub keeps it once by its SHA-256,
 * checks by its first bytes that it is a PNG, JPEG, WebP or GIF image, and answers its hash for a version to name.
 */
export async function uploadImage(project: string, file: File): Promise<DocsUploadedImage> {
  const headers = await csrfHeaders(browserApi());
  let response: Response;
  try {
    response = await fetch(`/v1/x/docs/${encodeURIComponent(project)}/images`, {
      method: "POST",
      body: file,
      headers: { ...headers, accept: "application/json", "content-type": file.type || "application/octet-stream" },
      credentials: "same-origin",
    });
  } catch (cause) {
    throw networkError(cause);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw fromResponse(response, body);
  return body as DocsUploadedImage;
}
