import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of the core plugin canvas (evo_agents.core_plugins.canvas.server, under /v1/x/canvas/{project}/),
 * as TanStack Query options the server (prefetch) and the browser share, and the writes the browser makes with the
 * session's X-Evo-CSRF header. Every key sits under the project's plugin keys, so one invalidation of a canvas reloads
 * its claims, proposals and baselines.
 */
type Schemas = components["schemas"];
export type CanvasSummary = Schemas["CanvasSummary"];
export type CanvasDetail = Schemas["CanvasDetail"];
export type CanvasClaim = Schemas["CanvasClaim"];
export type CanvasClaimDetail = Schemas["CanvasClaimDetail"];
export type CanvasContent = Schemas["CanvasContent"];
export type CanvasRev = Schemas["CanvasRev"];
export type CanvasEvidence = Schemas["CanvasEvidence"];
export type CanvasJudging = Schemas["CanvasJudging"];
export type CanvasProposal = Schemas["CanvasProposal"];
export type CanvasFieldChange = Schemas["CanvasFieldChange"];
export type CanvasTestCard = Schemas["CanvasTestCard"];
export type CanvasTestCardIn = Schemas["CanvasTestCardIn"];
export type CanvasChange = Schemas["CanvasChangeIn"];
export type CanvasEvidenceIn = Schemas["CanvasEvidenceIn"];
export type CanvasIn = Schemas["CanvasIn"];
export type CanvasBaselineSummary = Schemas["CanvasBaselineSummary"];
export type CanvasBaseline = Schemas["CanvasBaseline"];
export type CanvasBaselineItem = Schemas["CanvasBaselineItem"];
export type CanvasDecisionIn = Schemas["CanvasBaselineDecisionIn"];
export type CanvasProvenance = Schemas["CanvasProvenance"];
export type Cell = CanvasClaim["cell"];
export type ClaimStatus = CanvasClaim["status"];
export type Confidence = CanvasClaim["confidence"];
export type Delivery = CanvasContent["delivery"];
export type EvidenceKind = CanvasEvidence["kind"];
export type AddedEvidenceKind = CanvasEvidenceIn["kind"];
export type Direction = CanvasEvidence["direction"];
export type BaselineStatus = CanvasBaseline["status"];
export type ItemKind = CanvasBaselineItem["kind"];
export type ItemDecision = CanvasDecisionIn["decision"];
export type Verdict = NonNullable<CanvasJudging["verdict"]>;

export const PLUGIN = "canvas";
/** The plugin's page in the project's menu (its manifest's PAGES): the canvases, with everything else under it. */
export const PAGE = "canvases";

export const canvasKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", PLUGIN] as const,
  canvases: (project: string) => [...canvasKeys.all(project), "canvases"] as const,
  canvas: (project: string, slug: string) => [...canvasKeys.canvases(project), slug] as const,
  detail: (project: string, slug: string, archived: boolean) => [...canvasKeys.canvas(project, slug), "detail", archived] as const,
  claim: (project: string, slug: string, ref: string) => [...canvasKeys.canvas(project, slug), "claim", ref] as const,
  judgings: (project: string, slug: string, ref: string) => [...canvasKeys.claim(project, slug, ref), "judgings"] as const,
  proposals: (project: string, slug: string) => [...canvasKeys.canvas(project, slug), "proposals"] as const,
  baselines: (project: string, slug: string) => [...canvasKeys.canvas(project, slug), "baselines"] as const,
  baseline: (project: string, slug: string, number: number) => [...canvasKeys.baselines(project, slug), number] as const,
};

export const canvasesQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: canvasKeys.canvases(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/canvas/{project}/canvases", { params: { path: { project } }, signal })),
  });

/** A canvas whole: its nine cells in order, each with its claims by place; archived claims too with `archived`. */
export const canvasQuery = (api: ApiSource, project: string, slug: string, archived = false) =>
  queryOptions({
    queryKey: canvasKeys.detail(project, slug, archived),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/canvas/{project}/canvases/{slug}", {
          params: { path: { project, slug }, query: archived ? { archived } : {} },
          signal,
        }),
      ),
  });

/** A claim whole: every rev with what it changed and who wrote it, and its evidence with whether each piece is real. */
export const claimQuery = (api: ApiSource, project: string, slug: string, ref: string) =>
  queryOptions({
    queryKey: canvasKeys.claim(project, slug, ref),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}", { params: { path: { project, slug, ref } }, signal })),
  });

/** What changed in a claim from rev `base` to rev `head`, field by field. */
export const claimDiffQuery = (api: ApiSource, project: string, slug: string, ref: string, base: number, head: number) =>
  queryOptions({
    queryKey: [...canvasKeys.claim(project, slug, ref), "diff", base, head] as const,
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}/diff", {
          params: { path: { project, slug, ref }, query: { base, head } },
          signal,
        }),
      ),
  });

/** What @strategist's judging runs read of a claim and the verdict on each, the newest first. */
export const judgingsQuery = (api: ApiSource, project: string, slug: string, ref: string) =>
  queryOptions({
    queryKey: canvasKeys.judgings(project, slug, ref),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}/judgings", { params: { path: { project, slug, ref } }, signal }),
      ),
  });

/** The proposals that wait for a writer: new claims and changes an agent or the value check proposed. */
export const proposalsQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: canvasKeys.proposals(project, slug),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/canvas/{project}/canvases/{slug}/proposals", { params: { path: { project, slug } }, signal })),
  });

export const baselinesQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: canvasKeys.baselines(project, slug),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/canvas/{project}/canvases/{slug}/baselines", { params: { path: { project, slug } }, signal })),
  });

export const baselineQuery = (api: ApiSource, project: string, slug: string, number: number) =>
  queryOptions({
    queryKey: canvasKeys.baseline(project, slug, number),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/x/canvas/{project}/canvases/{slug}/baselines/{number}", { params: { path: { project, slug, number } }, signal }),
      ),
  });

// Writes, each with the session's CSRF header

export async function createCanvas(api: ApiClient, project: string, body: CanvasIn) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/canvas/{project}/canvases", { params: { path: { project } }, body, headers }));
}

export async function exportCanvas(api: ApiClient, project: string, slug: string) {
  return call(api.GET("/v1/x/canvas/{project}/canvases/{slug}/export", { params: { path: { project, slug } } }));
}

/** Apply `changes` in one go, each claim at the rev it was read at; a 409 names the claims changed since. */
export async function editClaims(api: ApiClient, project: string, slug: string, changes: CanvasChange[], reason: string) {
  const headers = await csrfHeaders(api);
  const body = { changes, reason };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/edit", { params: { path: { project, slug } }, body, headers }));
}

/** Take back `rev`, the claim's current one, by writing the content of the rev before it as a new rev. */
export async function undoClaim(api: ApiClient, project: string, slug: string, claim: string, rev: number, reason: string) {
  const headers = await csrfHeaders(api);
  const body = { revs: [{ claim, rev }], reason };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/undo", { params: { path: { project, slug } }, body, headers }));
}

export async function addEvidence(api: ApiClient, project: string, slug: string, ref: string, body: CanvasEvidenceIn) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, ref } };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}/evidence", { params, body, headers }));
}

export async function withdrawEvidence(api: ApiClient, project: string, slug: string, ref: string, id: number, reason: string) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, ref, evidence_id: id } };
  const path = "/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}/evidence/{evidence_id}/withdraw";
  return call(api.POST(path, { params, body: { reason }, headers }));
}

export async function confirmEvidence(api: ApiClient, project: string, slug: string, ref: string, id: number) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, ref, evidence_id: id } };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/claims/{ref}/evidence/{evidence_id}/confirm", { params, headers }));
}

export async function acceptProposal(api: ApiClient, project: string, slug: string, id: number) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, proposal_id: id } };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/proposals/{proposal_id}/accept", { params, headers }));
}

export async function rejectProposal(api: ApiClient, project: string, slug: string, id: number, reason: string) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, proposal_id: id } };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/proposals/{proposal_id}/reject", { params, body: { reason }, headers }));
}

/** Open a baseline of one claim at once, for another writer to decide (push-now). */
export async function openBaseline(api: ApiClient, project: string, slug: string, claim: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/baselines", { params: { path: { project, slug } }, body: { claim }, headers }));
}

export async function decideItem(api: ApiClient, project: string, slug: string, number: number, ref: string, body: CanvasDecisionIn) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug, number, ref } };
  return call(api.POST("/v1/x/canvas/{project}/canvases/{slug}/baselines/{number}/items/{ref}/decide", { params, body, headers }));
}
