import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { pluginKeys } from "@/lib/plugin-queries";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of the core plugin srs (evo_agents.core_plugins.srs.server, under /v1/x/srs/{project}/), as
 * TanStack Query options the server (prefetch) and the browser share, and the writes the browser makes with the
 * session's X-Evo-CSRF header. Every key sits under the project's plugin keys, so one invalidation of a document
 * reloads its versions, comparisons and comments.
 */
type Schemas = components["schemas"];
export type SrsDocumentSummary = Schemas["SrsDocumentSummary"];
export type SrsDocument = Schemas["SrsDocument"];
export type SrsVersion = Schemas["SrsVersion"];
export type SrsVersionDetail = Schemas["SrsVersionDetail"];
export type SrsReview = Schemas["SrsReview"];
export type SrsItem = Schemas["SrsItem"];
export type SrsProblem = Schemas["SrsProblem"];
export type SrsDiff = Schemas["SrsDiff"];
export type SrsDiffItem = Schemas["SrsDiffItem"];
export type SrsComment = Schemas["SrsComment"];
export type SrsImportResult = Schemas["SrsImportResult"];
export type SrsPreview = Schemas["SrsPreview"];
export type SrsSave = Schemas["SrsSave"];
export type SrsChange = Schemas["SrsChange"];
export type SrsReviewIn = Schemas["SrsReviewIn"];
export type SrsCommentIn = Schemas["SrsCommentIn"];
export type SrsImport = Schemas["SrsImport"];
export type SrsFinding = Schemas["SrsFinding"];
export type SrsFindingSource = Schemas["SrsFindingSource"];
export type SrsReviewRun = Schemas["SrsReviewRun"];
export type VersionState = SrsVersion["state"];
export type ItemKind = SrsItem["kind"];
export type DocumentKind = SrsDocumentSummary["kind"];
export type ReviewRole = NonNullable<SrsReview["role"]>;

export const PLUGIN = "srs";

export const srsKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", PLUGIN] as const,
  documents: (project: string) => [...srsKeys.all(project), "documents"] as const,
  document: (project: string, slug: string) => [...srsKeys.documents(project), slug] as const,
  version: (project: string, slug: string, version: number | null) =>
    [...srsKeys.document(project, slug), "version", version ?? "latest"] as const,
  diff: (project: string, slug: string, from: number, to: number) =>
    [...srsKeys.document(project, slug), "diff", from, to] as const,
  comments: (project: string, slug: string) => [...srsKeys.document(project, slug), "comments"] as const,
  findings: (project: string, slug: string) => [...srsKeys.document(project, slug), "findings"] as const,
};

export const srsDocumentsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: srsKeys.documents(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/srs/{project}/documents", { params: { path: { project } }, signal })),
  });

/** A document with its versions and one of them whole: `version`, or the latest when null. */
export const srsDocumentQuery = (api: ApiSource, project: string, slug: string, version: number | null) =>
  queryOptions({
    queryKey: srsKeys.version(project, slug, version),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/srs/{project}/documents/{slug}", {
          params: { path: { project, slug }, query: version === null ? {} : { version } },
          signal,
        }),
      ),
  });

export const srsDiffQuery = (api: ApiSource, project: string, slug: string, from: number, to: number) =>
  queryOptions({
    queryKey: srsKeys.diff(project, slug, from, to),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/srs/{project}/documents/{slug}/diff", {
          params: { path: { project, slug }, query: { from, to } },
          signal,
        }),
      ),
  });

export const srsCommentsQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: srsKeys.comments(project, slug),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/srs/{project}/documents/{slug}/comments", { params: { path: { project, slug } }, signal })),
  });

/** The conflicts @ba found in a document, oldest first, with their sources and how each was settled. */
export const srsFindingsQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: srsKeys.findings(project, slug),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/srs/{project}/documents/{slug}/findings", { params: { path: { project, slug } }, signal })),
  });

/** The config the plugin gets in the project (roles and approval policy), which every member reads. */
export const srsConfigQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: pluginKeys.effective(project, PLUGIN),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/plugins/{name}/config/effective", {
          params: { path: { project, name: PLUGIN } },
          signal,
        }),
      ),
  });

// Writes, each with the session's CSRF header

export async function exportMarkdown(api: ApiClient, project: string, slug: string, version: number) {
  return call(
    api.GET("/v1/x/srs/{project}/documents/{slug}/export", { params: { path: { project, slug }, query: { version } } }),
  );
}

export async function saveVersion(api: ApiClient, project: string, slug: string, body: SrsSave) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/srs/{project}/documents/{slug}/versions", { params: { path: { project, slug } }, body, headers }));
}

export async function submitVersion(api: ApiClient, project: string, slug: string, number: number) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, number } };
  return call(api.POST("/v1/x/srs/{project}/documents/{slug}/versions/{number}/submit", { params, headers }));
}

export async function reviewVersion(api: ApiClient, project: string, slug: string, number: number, body: SrsReviewIn) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, number } };
  return call(api.POST("/v1/x/srs/{project}/documents/{slug}/versions/{number}/reviews", { params, body, headers }));
}

export async function addComment(api: ApiClient, project: string, slug: string, body: SrsCommentIn) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/srs/{project}/documents/{slug}/comments", { params: { path: { project, slug } }, body, headers }));
}

export async function resolveComment(api: ApiClient, project: string, slug: string, commentId: number) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, comment_id: commentId } };
  return call(api.POST("/v1/x/srs/{project}/documents/{slug}/comments/{comment_id}/resolve", { params, headers }));
}

export async function importMarkdown(api: ApiClient, project: string, body: SrsImport) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/srs/{project}/import", { params: { path: { project } }, body, headers }));
}
